#!/usr/bin/env python3
"""Resume one approved batch without duplicate jobs or repository bursts.

Use the batch's reviewed launcher for validation/preflight/HTTP/atomic I/O.
GitHub guidance: honor Retry-After, serialize mutations, and use bounded
increasing delays on secondary limits:
https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RATE_MESSAGE = "you have created too many repositories, too quickly"
DELAYS = (300, 900, 1800)


class UnknownOutcome(RuntimeError):
    pass


class CreationRateLimit(RuntimeError):
    def __init__(self, retry_after=0):
        super().__init__("GitHub repository creation is rate limited")
        self.retry_after = retry_after


def load_launcher(batch_dir: Path):
    spec = importlib.util.spec_from_file_location("approved_batch_launcher", batch_dir / "launch_pairs.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Approved launcher is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready(job: dict) -> bool:
    return job.get("baseline_prepared") is True and bool(re.fullmatch(r"[a-f0-9]{40}", str(job.get("baseline_sha", ""))))


def read_only_post(base_url: str, path: str, body: dict) -> dict:
    """Desk reads are JSON POSTs; job.error is stored data, not an envelope.

    The original launcher's generic post() intentionally rejects any error
    field and therefore cannot read a failed job's successful HTTP response.
    """
    if path not in ("/api/job", "/api/jobs"):
        raise ValueError("Read-only helper only accepts job/inventory routes")
    request = Request(base_url.rstrip("/") + path,
                      data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=600) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError("Desk rejected read-only " + path)
    if path == "/api/job":
        if not isinstance(result.get("id"), str) or not isinstance(result.get("sides"), dict):
            raise RuntimeError("Desk returned an invalid job object")
    elif not isinstance(result.get("jobs"), list) or any(not isinstance(job, dict) for job in result["jobs"]):
        raise RuntimeError("Desk returned an invalid job inventory")
    return result


class PacedSubmission:
    def __init__(self, batch_dir: Path, desk_url: str, *, launcher=None, clock=time.time, sleep=time.sleep):
        self.folder = batch_dir.resolve()
        self.launcher = launcher or load_launcher(self.folder)
        self.read_only_post = getattr(self.launcher, "read_only_post", read_only_post)
        self.url, self.clock, self.sleep, self.stop = desk_url, clock, sleep, False
        self.cards = self.launcher.read_json(self.folder / "create-requests.json")
        self.launcher.validate(self.cards)
        self.digest = hashlib.sha256(json.dumps(self.cards, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        self.receipt_path = self.folder / "submission-receipt.json"
        self.state_path = self.folder / "paced-submission-state.json"
        self.receipt = self.launcher.read_json(self.receipt_path) if self.receipt_path.exists() else {"batch_sha256": self.digest, "entries": {}}
        self.state = self.launcher.read_json(self.state_path) if self.state_path.exists() else {
            "schema": 1, "batch_sha256": self.digest, "status": "starting", "repos": {}, "next_create_at": 0,
        }
        if self.receipt.get("batch_sha256") != self.digest or self.state.get("batch_sha256") != self.digest:
            raise RuntimeError("Batch cards differ from the frozen submission receipt/state")
        entries = self.receipt.get("entries")
        names = {card["github_repo"] for card in self.cards}
        if not isinstance(entries, dict) or not set(entries).issubset(names):
            raise RuntimeError("Receipt contains unauthorized repositories")
        for entry in entries.values():
            if not isinstance(entry, dict) or entry.get("status") not in ("queued", "registered", "prepare_failed", "pending"):
                raise RuntimeError("Receipt contains an unsupported outcome")
        self.connection_id = self.receipt.get("cli_connection_id", "")

    def save(self, status: str, repo: str = "") -> None:
        previous = (self.state.get("status"), self.state.get("active_repo", ""))
        self.state.update(status=status, active_repo=repo, updated_at=self.clock())
        self.launcher.atomic_json(self.state_path, self.state)
        if previous != (status, repo):
            print(status + (" " + repo if repo else ""), flush=True)

    def save_receipt(self) -> None:
        self.launcher.atomic_json(self.receipt_path, self.receipt)

    def post(self, path: str, body: dict, *, mutation=False) -> dict:
        try:
            if path in ("/api/job", "/api/jobs"):
                return self.read_only_post(self.url, path, body)
            return self.launcher.post(self.url, path, body)
        except HTTPError as exc:
            try:
                text = json.loads(exc.read().decode("utf-8")).get("error", "")
            except (ValueError, UnicodeError, AttributeError):
                text = ""
            if RATE_MESSAGE in str(text).lower():
                try:
                    retry_after = max(0, float(exc.headers.get("Retry-After", 0)))
                except (ValueError, TypeError):
                    retry_after = 0
                raise CreationRateLimit(retry_after) from None
            raise RuntimeError(f"Desk rejected {path}: HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            error = UnknownOutcome if mutation else RuntimeError
            raise error(f"Desk {path} outcome unavailable ({type(exc).__name__})") from None

    def wait_until(self, timestamp: float, status: str, repo: str) -> None:
        if timestamp > self.clock():
            self.save(status, repo)
        while not self.stop and timestamp > self.clock():
            self.sleep(min(5, timestamp - self.clock()))
        if self.stop:
            raise UnknownOutcome("Submission interrupted; receipt/state retained")

    def matches(self, job: dict, card: dict) -> bool:
        repo = card["github_repo"]
        owner = card["github_owner"]
        return (job.get("github_repo") in (repo, f"{owner}/{repo}")
                and job.get("prompt") == card["prompt"] and job.get("agent") == card["agent"]
                and job.get("codex_model") == card["cli_model"]
                and job.get("source_mode") == card["source_mode"]
                and str(job.get("github_owner", "")).lower() == str(card["github_owner"]).lower()
                and job.get("github_private") is False
                and (job.get("cli_connection") or {}).get("id") == self.connection_id)

    def frozen_job(self, job_id: str, card: dict) -> dict:
        if not re.fullmatch(r"pair-[a-f0-9]{10}", str(job_id)):
            raise RuntimeError("Known job has no valid frozen ID")
        job = self.post("/api/job", {"id": job_id})
        if job.get("id") != job_id or not self.matches(job, card):
            raise RuntimeError("Known job does not match the approved card/connection")
        return job

    def schedule_rate(self, repo: str, job_id: str, *, retry_after=0, observed_at=None) -> None:
        info = self.state["repos"].setdefault(repo, {})
        failures = int(info.get("rate_failures", 0)) + 1
        info.update(job_id=job_id, rate_failures=failures, status="rate_backoff", classification="github-repository-creation-limit")
        if failures > len(DELAYS):
            self.save("rate_limit_exhausted", repo)
            raise RuntimeError("Repository creation rate limit persisted beyond three bounded retries")
        now = self.clock() if observed_at is None else observed_at
        info["next_attempt_at"] = now + max(DELAYS[failures - 1], retry_after)
        self.save("rate_backoff", repo)

    def mark_queued(self, card: dict, job_id: str) -> None:
        self.state["repos"].setdefault(card["github_repo"], {}).update(job_id=job_id, status="queued")
        self.state["next_create_at"] = self.clock() + 120
        self.save("queued", card["github_repo"])
        # Persist the cooldown before a queued receipt can make a restart skip
        # this job. A crash between writes then causes only a safe read-reconcile.
        self.receipt["entries"][card["github_repo"]] = {"status": "queued", "job_id": job_id}
        self.save_receipt()

    def recover_known(self, card: dict, job_id: str) -> None:
        repo = card["github_repo"]
        job = self.frozen_job(job_id, card)
        sides = job.get("sides", {})
        if ready(job) and sides and all(side.get("status") in ("queued", "running", "done", "failed") for side in sides.values()):
            self.mark_queued(card, job_id)
            return
        info = self.state["repos"].setdefault(repo, {})
        if info.get("status") == "unknown_enqueue":
            raise UnknownOutcome("Known enqueue outcome is still unresolved; refusing another preparation mutation")
        if job.get("error") and RATE_MESSAGE not in str(job["error"]).lower():
            raise RuntimeError("Known job has a non-rate preparation error; operator recovery is required")
        if not info.get("rate_failures") and RATE_MESSAGE in str(job.get("error", "")).lower():
            observed = self.clock()
            try:
                observed = datetime.fromisoformat(job["updated_at"].replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError):
                pass
            self.schedule_rate(repo, job_id, observed_at=observed)
        while not self.stop:
            deadline = max(info.get("next_attempt_at", 0), self.state.get("next_create_at", 0))
            self.wait_until(deadline, "rate_backoff" if info.get("next_attempt_at", 0) > self.clock() else "paced_wait", repo)
            if int(info.get("rate_failures", 0)) > len(DELAYS):
                raise RuntimeError("Repository creation retry budget exhausted")
            current = self.frozen_job(job_id, card)
            if ready(current) and current.get("sides") and all(
                    side.get("status") in ("queued", "running", "done", "failed") for side in current["sides"].values()):
                self.mark_queued(card, job_id)
                return
            info.update(status="unknown_enqueue", dispatched_at=self.clock())
            # A crash after sending the POST must retain its unknown outcome.
            self.save("recovering_known_job", repo)
            try:
                self.post("/api/job_action", {"job": job_id, "action": "enqueue"}, mutation=True)
            except CreationRateLimit as exc:
                self.schedule_rate(repo, job_id, retry_after=exc.retry_after)
                continue
            except UnknownOutcome:
                self.save("unknown_enqueue", repo)
                raise
            except RuntimeError:
                info["status"] = "known_error"
                self.save("needs_attention", repo)
                raise
            job = self.frozen_job(job_id, card)
            if not ready(job):
                raise RuntimeError("Known job enqueue returned without a prepared frozen baseline")
            self.mark_queued(card, job_id)
            return
        raise UnknownOutcome("Submission interrupted")

    def reconcile_pending(self, card: dict) -> None:
        jobs = self.post("/api/jobs", {}).get("jobs", [])
        matches = [job for job in jobs if self.matches(job, card)]
        if len(matches) != 1:
            raise UnknownOutcome("Pending creation has no unique matching job; refusing another job_create")
        job_id = matches[0].get("id", "")
        self.receipt["entries"][card["github_repo"]] = {"status": "prepare_failed", "job_id": job_id}
        self.save_receipt()
        self.recover_known(card, job_id)

    def create_one(self, card: dict) -> None:
        repo = card["github_repo"]
        self.wait_until(self.state.get("next_create_at", 0), "paced_wait", repo)
        current = self.launcher.preflight(self.url, [card])
        if current != self.connection_id:
            raise RuntimeError("CLI connection changed since this batch started")
        self.receipt["entries"][repo] = {"status": "pending"}
        self.save_receipt()
        self.save("creating", repo)
        result = self.post("/api/job_create", {**card, "cli_connection_id": self.connection_id}, mutation=True)
        job_id = result.get("job", "")
        if not re.fullmatch(r"pair-[a-f0-9]{10}", str(job_id)):
            raise UnknownOutcome("Creation response contains no valid job ID; pending retained")
        success = job_id in result.get("prepared", []) and not result.get("prepare_error")
        self.receipt["entries"][repo] = {"status": "registered" if success else "prepare_failed", "job_id": job_id}
        self.save_receipt()
        if success:
            if not ready(self.frozen_job(job_id, card)):
                self.receipt["entries"][repo]["status"] = "prepare_failed"
                self.save_receipt()
                raise RuntimeError("Creation response did not bind a prepared baseline")
            self.mark_queued(card, job_id)
        elif RATE_MESSAGE in str(result.get("prepare_error", "")).lower():
            self.schedule_rate(repo, job_id)
            self.recover_known(card, job_id)
        else:
            raise RuntimeError("Created job has a known non-rate preparation failure")

    def submit(self) -> int:
        active = self.launcher.preflight(self.url, [])
        if self.connection_id and active != self.connection_id:
            raise RuntimeError("CLI connection changed since this batch started")
        self.connection_id = active
        if not active:
            raise RuntimeError("No frozen CLI connection is available")
        self.receipt["cli_connection_id"] = active
        self.save_receipt()
        for card in self.cards:
            if self.stop:
                raise UnknownOutcome("Submission interrupted")
            entry = self.receipt["entries"].get(card["github_repo"])
            if entry and entry["status"] == "queued":
                continue
            if entry and entry["status"] == "pending":
                self.reconcile_pending(card)
            elif entry:
                self.recover_known(card, entry.get("job_id", ""))
            else:
                self.create_one(card)
        self.save("complete")
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--desk-url", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("Paced submission must run on the Linux server")
    import fcntl
    with (args.batch_dir / "submission.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another batch launcher holds submission.lock") from None
        launcher = PacedSubmission(args.batch_dir, args.desk_url)
        def stop(signum, frame):
            launcher.stop = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            return launcher.submit()
        except Exception as exc:
            launcher.state["error_class"] = type(exc).__name__
            launcher.save("unknown_outcome" if isinstance(exc, UnknownOutcome) else "needs_attention")
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
