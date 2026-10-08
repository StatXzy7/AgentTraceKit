"""Keep Linux probes/recording on the server; run authorized grading on the desktop."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tarfile
import time

from .authorized_review_model import authorized_model
from .batch_pipeline import (BatchPipeline, MODEL, SessionUnavailable, UnknownOutcome, atomic_json,
                             classify_provider_failure, read_json, read_schema_output, receipt_jobs,
                             sha256, validate_output_schema)
from .desk_store import DEFAULT_HOME, utc_now


class LocalReviewPipeline(BatchPipeline):
    def __init__(self, *args, review_model: str, authorization_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        old = self.state
        self.review_model, self.review_authorization_id = review_model, authorization_id
        ids = receipt_jobs(read_json(self.batch_dir / "submission-receipt.json"))
        if len(ids) != 20:
            raise RuntimeError("Local evaluation must bind the complete approved 20-task batch")
        for job_id in ids:
            authorized_model({"id": job_id, "agent": "codex", "codex_model": MODEL}, review_model, authorization_id)
        self.state_path = self.batch_dir / "local-pipeline-state.json"
        if self.state_path.exists():
            self.state = read_json(self.state_path)
            if self.state.get("model") != review_model or self.state.get("authorization_id") != authorization_id:
                raise RuntimeError("Local evaluation identity changed")
        else:
            self.state = {"schema": 1, "status": "waiting_submission", "jobs": {}, "created_at": utc_now(),
                          "model": review_model, "generation_model": MODEL, "authorization_id": authorization_id,
                          "origin": "ai-authorized", "human_validity_required": True,
                          "scoring_sha256": self.scoring_digest, "batch_sha256": self.requests_digest,
                          "prior_cloud_state_sha256": sha256(self.batch_dir / "pipeline-state.json")}
            # A new authorization has its own bounded postprocessing budget.
            # Earlier exhausted cloud attempts remain in the original state/logs.
            for job_id in ids:
                previous = old.get("jobs", {}).get(job_id, {})
                if previous.get("status") in {"needs_validity", "needs_recovery", "complete"}:
                    self.state["jobs"][job_id] = copy.deepcopy(previous)
                else:
                    self.state["jobs"][job_id] = {"status": "waiting_generation", "attempt": 0,
                                                  "prior_cloud_entry": copy.deepcopy(previous)}
        if self.state.get("scoring_sha256") != self.scoring_digest or self.state.get("batch_sha256") != self.requests_digest:
            raise RuntimeError("Local evaluation batch or scoring requirements changed")

    def _schema_compatibility_enabled(self, job):
        return False

    def _seed_schema_compatibility(self, job, entry):
        return None

    def _terminal_reconciliation(self, job, entry):
        run_id = entry.get("run_id", "")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{8}", run_id):
            return None
        folder = self.desk_home / "evidence" / job["id"] / "auto-review" / run_id
        requests = sorted(folder.glob("*/relay-request.json"))
        if not requests:
            return None
        proof = []
        for path in requests:
            request = read_json(path)
            if (request.get("job") != job["id"] or request.get("run_id") != run_id
                    or request.get("directory") != str(path.parent)):
                raise UnknownOutcome("Local reconciliation request changed")
            if not (path.parent / "result.json").is_file():
                return None
            self._validate_terminal(path.parent, request)
            proof.append({"request": str(path), "request_sha256": sha256(path),
                          "result_sha256": sha256(path.parent / "result.json")})
        return proof

    def _resume_completed_job(self, job, entry):
        proof = entry.get("local_terminal_reconciliation")
        return bool(proof and proof == self._terminal_reconciliation(job, entry))

    def _finish_review(self, job, entry):
        pending_phase = entry.get("local_pending_phase", "")
        phase = entry.get("phase", "")
        model_checkpoint = isinstance(pending_phase, str) and (
            (not phase and pending_phase.startswith("prepare"))
            or (phase == "evaluating" and pending_phase.startswith("evaluate")))
        if (entry.get("status") == "unknown" and not entry.get("review_evidence")
                and model_checkpoint
                and isinstance(pending_phase, str)
                and re.fullmatch(r"(?:prepare|evaluate)(?:-retry-[23])?", pending_phase)):
            proof = self._terminal_reconciliation(job, entry)
            if proof:
                entry["local_terminal_reconciliation"] = proof
                entry["status"] = "running"
                self.save()
                self.process_one(job, entry)
                return
        return super()._finish_review(job, entry)

    def _validate_terminal(self, directory, request):
        result = read_json(directory / "result.json")
        if (result.get("model") != self.review_model or result.get("authorization_id") != self.review_authorization_id
                or result.get("execution_host") != "local-windows"
                or result.get("request_id") != request["id"]
                or result.get("schema_sha256") != request["schema_sha256"]
                or result.get("prompt_sha256") != request["prompt_sha256"]
                or sha256(directory / "schema.json") != request["schema_sha256"]
                or sha256(directory / "prompt.txt") != request["prompt_sha256"]
                or sha256(directory / "process.json") != result.get("process_sha256")
                or sha256(directory / "events.jsonl") != result.get("events_sha256")):
            raise UnknownOutcome("Local evaluation result identity mismatch")
        process = read_json(directory / "process.json")
        command = process.get("command", [])
        if (not isinstance(command, list) or any(not isinstance(arg, str) for arg in command)
                or command.count("--model") != 1 or command.index("--model") + 1 >= len(command)):
            raise UnknownOutcome("Local evaluation command proof is malformed")
        if (process.get("model") != self.review_model or process.get("request_id") != request["id"]
                or process.get("execution_host") != "local-windows" or type(process.get("pid")) is not int
                or process["pid"] <= 0 or "--model" not in command
                or command[command.index("--model") + 1] != self.review_model):
            raise UnknownOutcome("Local evaluation process proof mismatch")
        events = []
        for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        session = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), "")
        completed = any(e.get("type") == "turn.completed" for e in events)
        if (not session or result.get("session_id") != session or result.get("completed") is not completed
                or type(result.get("exit_code")) is not int
                or result.get("failure_classification") != classify_provider_failure(events)):
            raise UnknownOutcome("Local evaluation terminal trajectory mismatch")
        if result["exit_code"] == 0 and completed:
            if sha256(directory / "final.json") != result.get("final_sha256"):
                raise UnknownOutcome("Local evaluation final hash mismatch")
        return result

    def _run_codex(self, job, folder, phase, prompt, schema, **kwargs):
        for directory in folder.glob(phase + "*"):
            if directory.is_dir() and (directory / "result.json").is_file():
                self._validate_terminal(directory, read_json(directory / "relay-request.json"))
        return super()._run_codex(job, folder, phase, prompt, schema, **kwargs)

    def _run_codex_once(self, job, folder, phase, prompt, schema, *, images=None, local_validation=False):
        validate_output_schema(schema)
        if local_validation:
            raise RuntimeError("The local relay requires native structured output")
        authorization = authorized_model(job, self.review_model, self.review_authorization_id)
        directory = folder / phase
        directory.mkdir(exist_ok=False)
        atomic_json(directory / "schema.json", schema)
        prompt += (
            "\n本次由用户新增授权的本地 Codex CLI 完成辅助评价。生成模型仍为 auto_model/urm，不得续接生成。"
            f"\n真正的 Linux 冻结工作目录：{folder}。本地 A/、B/仅是供读取的镜像。"
            "本地 .atk-execution-transport.json 仅记录可用的 SSH 主机别名与远端 Python 路径，可读取以执行验证。"
            "所有实际构建、算法探针与录像脚本均须通过该 SSH 主机在此 Linux 目录执行；"
            "每条命令先明确 cwd，只允许新增 .atk-review 内的辅助脚本，不得更改任何受测已有源码。"
            "本地端运行不算 Linux 验证。Linux Python 路径由 worker 私有配置指定。"
            "本地 generation-evidence.json 给出原生成轨迹和所有尝试路径，必须读取原轨迹；"
            "failed侧必须检查全部尝试和事后冻结出处。不要读取 SSH/CLI凭据、连接配置、环境或其他批次。"
            "不要创建聊天、自动化、子代理或发送外部消息。仅完成当前prepare/evaluate。"
            "最后严格返回规定JSON；不能把工具报错、辅助探针中止或只读取源码写成全套实测通过。"
            f"\n实际评价来源：本地 Codex CLI / {self.review_model}；非人工。"
        )
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        # Only approved artifacts and evaluation material travel to the desktop.
        # Raw generation traces remain authoritative on Linux, with exact paths.
        generation = {"job": job["id"], "generation_model": MODEL, "sides": {
            name: {key: side.get(key) for key in ("status", "exit_code", "error", "session_id", "jsonl_local",
                                                "head_sha", "attempts", "failure_capture", "demo")}
            for name, side in job["sides"].items()}}
        atomic_json(folder / "generation-evidence.json", generation)
        request_id = f"{job['id']}-{folder.name}-{phase}"
        queue = self.batch_dir / "local-review-relay" / request_id
        queue.mkdir(parents=True, exist_ok=False)
        request = {"schema": 1, "id": request_id, "job": job["id"], "run_id": folder.name, "phase": phase,
                   "model": self.review_model, "authorization": authorization, "workspace": str(folder),
                   "directory": str(directory), "schema_sha256": sha256(directory / "schema.json"),
                   "prompt_sha256": sha256(directory / "prompt.txt"), "images": [str(p) for p in images or []],
                   "created_at": utc_now(), "deadline": time.time() + self.phase_timeout}
        bundle = queue / "workspace.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            for path in sorted(folder.rglob("*")):
                relative = path.relative_to(folder)
                if not path.is_file() or path.is_symlink() or path.suffix == ".mp4":
                    continue
                if any(part in {"node_modules", ".git", "__pycache__", ".venv", "venv"} for part in relative.parts):
                    continue
                if relative.parts[0] in {"A-preparation-attempt", "B-preparation-attempt"}:
                    continue
                if path.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimeError("Local grading bundle contains an oversized file")
                archive.add(path, arcname=relative.as_posix(), recursive=False)
        request.update(bundle_sha256=sha256(bundle), bundle_size=bundle.stat().st_size)
        atomic_json(directory / "relay-request.json", request)
        entry = self.state["jobs"][job["id"]]
        entry["local_pending_phase"] = phase
        self.save()
        atomic_json(queue / "request.json", request)
        while not self.stop:
            result_path = directory / "result.json"
            if result_path.is_file():
                result = self._validate_terminal(directory, request)
                # Retain the last dispatched phase through later checkpoints:
                # a stop before attempts/prepared/scored saves can then reconcile
                # the same terminal session without a new model dispatch.
                if result.get("exit_code") != 0 or not result.get("completed"):
                    if result.get("failure_classification") == "transient-provider":
                        raise SessionUnavailable("Local Codex transient failure; full evidence retained")
                    raise RuntimeError("Local Codex did not complete structured evaluation")
                return read_schema_output(directory / "final.json", schema)
            if time.time() > request["deadline"]:
                raise UnknownOutcome("Local relay remains nonterminal; reconcile without redispatch")
            time.sleep(2)
        raise UnknownOutcome("Controller stopped while local evaluation may be active")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--scoring-file", type=Path, required=True)
    parser.add_argument("--desk-home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--model", required=True)
    parser.add_argument("--authorization-id", required=True)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("Recording/probe coordinator must run on Linux")
    pipeline = LocalReviewPipeline(args.batch_dir, "http://127.0.0.1:8765", args.scoring_file,
                                   desk_home=args.desk_home, review_model=args.model,
                                   authorization_id=args.authorization_id, retry_failed=True)
    return pipeline.run()


if __name__ == "__main__":
    raise SystemExit(main())
