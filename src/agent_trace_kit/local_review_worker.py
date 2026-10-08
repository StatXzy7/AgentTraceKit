"""Durable Windows Codex worker for one explicitly authorized Linux batch."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time

from .batch_pipeline import atomic_json, classify_provider_failure, read_json, sha256, verify_originals
from .desk_store import utc_now

# Importing the module grants no authority and has no production identity.
REMOTE_BATCH = REMOTE_EVIDENCE = MODEL = GENERATION_MODEL = AUTHORIZATION = ""
SSH_HOST = REMOTE_PYTHON = ""
JOBS: set[str] = set()
RECOVERY_AUTHORIZATIONS: dict = {}


def configure_worker(path: Path) -> None:
    """Load an operator-supplied allowlist before any transport or dispatch."""
    config = read_json(path)
    if not isinstance(config, dict) or config.get("schema") != 1:
        raise ValueError("Invalid private worker configuration")
    for key in ("model", "generation_model", "authorization_id", "ssh_host"):
        if not isinstance(config.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./@-]{0,199}", config[key]):
            raise ValueError(f"Invalid worker {key}")
    for key in ("remote_batch", "remote_evidence", "remote_python"):
        value = config.get(key)
        if (not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
                or ".." in PurePosixPath(value).parts or value == "/"):
            raise ValueError(f"Invalid worker {key}")
    jobs = config.get("job_ids")
    if (not isinstance(jobs, list) or len(jobs) != 20
            or any(not isinstance(job, str) or not re.fullmatch(r"pair-[a-f0-9]{10}", job) for job in jobs)
            or len(set(jobs)) != len(jobs)):
        raise ValueError("Worker configuration must bind 20 distinct approved jobs")
    recoveries = config.get("prelaunch_recoveries", {})
    if not isinstance(recoveries, dict) or any(not isinstance(v, dict) for v in recoveries.values()):
        raise ValueError("Invalid prelaunch recovery authorization")
    global REMOTE_BATCH, REMOTE_EVIDENCE, MODEL, GENERATION_MODEL, AUTHORIZATION
    global SSH_HOST, REMOTE_PYTHON, JOBS, RECOVERY_AUTHORIZATIONS
    REMOTE_BATCH, REMOTE_EVIDENCE = config["remote_batch"], config["remote_evidence"]
    MODEL, GENERATION_MODEL, AUTHORIZATION = config["model"], config["generation_model"], config["authorization_id"]
    SSH_HOST, REMOTE_PYTHON = config["ssh_host"], config["remote_python"]
    JOBS, RECOVERY_AUTHORIZATIONS = set(jobs), recoveries


def validate_request(request: dict, *, allow_expired: bool = False) -> None:
    job, run, phase = request.get("job"), request.get("run_id"), request.get("phase")
    if (job not in JOBS or not isinstance(run, str) or not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{8}", run)
            or not isinstance(phase, str) or not re.fullmatch(r"(?:prepare|evaluate)(?:-retry-[23])?", phase)):
        raise ValueError("Request is outside the approved batch")
    folder = f"{REMOTE_EVIDENCE}/{job}/auto-review/{run}"
    authorization = request.get("authorization", {})
    if (request.get("schema") != 1 or request.get("model") != MODEL
            or authorization.get("id") != AUTHORIZATION or authorization.get("model") != MODEL
            or authorization.get("generation_model") != GENERATION_MODEL
            or authorization.get("origin") != "human-authorized-local-evaluation"
            or request.get("id") != f"{job}-{run}-{phase}" or request.get("workspace") != folder
            or request.get("directory") != f"{folder}/{phase}"):
        raise ValueError("Request identity or authorization changed")
    for key in ("schema_sha256", "prompt_sha256", "bundle_sha256"):
        if not isinstance(request.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", request[key]):
            raise ValueError("Invalid request digest")
    if type(request.get("bundle_size")) is not int or not 0 < request["bundle_size"] <= 512 * 1024 * 1024:
        raise ValueError("Invalid request bundle size")
    if (type(request.get("deadline")) not in (int, float) or not math.isfinite(request["deadline"])
            or request["deadline"] < 0 or request["deadline"] >= time.time() + 2500
            or (not allow_expired and request["deadline"] <= time.time())):
        raise ValueError("Request deadline expired or invalid")
    images = request.get("images")
    if not isinstance(images, list) or len(images) > 6 or any(
            not isinstance(path, str) or not re.fullmatch(re.escape(folder) + r"/video-[AB]-[012]\.png", path)
            for path in images):
        raise ValueError("Image path is outside the authorized evaluation")


def extract_bundle(bundle: Path, folder: Path) -> None:
    total = 0
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 20000:
            raise ValueError("Too many evaluation bundle members")
        names = set()
        destinations = set()
        by_name = {item.name: item for item in members}
        sources = {}
        for item in members:
            path = PurePosixPath(item.name)
            if (not (item.isfile() or item.islnk()) or path.is_absolute() or ".." in path.parts or "\\" in item.name
                    or any(":" in part for part in path.parts) or item.name in names
                    or not path.parts or item.name != path.as_posix()):
                raise ValueError("Unsafe evaluation bundle member")
            names.add(item.name)
            destination = path.as_posix().casefold() if os.name == "nt" else path.as_posix()
            if (destination in destinations or (os.name == "nt" and any(
                    part.rstrip(" .") != part or part.split('.')[0].upper() in
                    {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
                    for part in path.parts))):
                raise ValueError("Evaluation bundle has conflicting Windows destinations")
            destinations.add(destination)
            source = item
            if item.islnk():
                link = PurePosixPath(item.linkname)
                if (link.is_absolute() or ".." in link.parts or "\\" in item.linkname
                        or any(":" in part for part in link.parts)):
                    raise ValueError("Unsafe evaluation hardlink target")
                source = by_name.get(item.linkname)
                # Materialize bytes from a regular member in this exact archive.
                # Never create OS links or follow symlinks/chains/outside paths.
                if source is None or not source.isfile():
                    raise ValueError("Evaluation hardlink must target a regular archive member")
            sources[item.name] = source
            total += source.size
            if total > 512 * 1024 * 1024:
                raise ValueError("Evaluation bundle too large")
        for destination in destinations:
            parts = destination.split('/')
            if any('/'.join(parts[:i]) in destinations for i in range(1, len(parts))):
                raise ValueError("Evaluation file conflicts with another member's parent directory")
        for item in members:
            target = folder.joinpath(*PurePosixPath(item.name).parts)
            if os.name == "nt":
                absolute = str(target.absolute())
                if not absolute.startswith("\\\\?\\"):
                    target = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                                  else "\\\\?\\" + absolute)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as destination, archive.extractfile(sources[item.name]) as source:
                shutil.copyfileobj(source, destination)


def validate_prelaunch_recovery(directory: Path, request: dict, *, before_dispatch: bool = True) -> None:
    """Permit only an explicitly authorized extraction failure before dispatch."""
    receipt = read_json(directory / "prelaunch-recovery.json")
    approved = RECOVERY_AUTHORIZATIONS.get(request["id"])
    if not isinstance(approved, dict) or receipt != approved:
        raise RuntimeError("Prelaunch recovery has no matching private authorization")
    expected = {
        "schema": 1, "kind": "verified-prelaunch-hardlink-extraction-failure",
        "job": request["job"], "run_id": request["run_id"], "phase": request["phase"],
        "request_id": request["id"],
        "request_sha256": sha256(directory / "request.json"),
        "bundle_sha256": request["bundle_sha256"], "cli_dispatched": False,
    }
    if (any(receipt.get(key) != value for key, value in expected.items())
            or not re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("legacy_worker_sha256", "")))):
        raise RuntimeError("Prelaunch recovery identity differs")
    if before_dispatch and (set(path.name for path in directory.iterdir())
            != {"request.json", "workspace.tar.gz", "workspace", "prelaunch-recovery.json"}
            or not (directory / "workspace").is_dir()
            or any((directory / "workspace").iterdir())):
        raise RuntimeError("Prelaunch recovery is not bound to the verified extraction failure")


def reserve_dispatch(directory: Path, request: dict) -> None:
    with (directory / "dispatch-intent.json").open("x", encoding="utf-8") as intent:
        json.dump({"request_id": request["id"], "model": MODEL,
                   "request_sha256": sha256(directory / "request.json"),
                   "created_at": utc_now(), "worker_pid": os.getpid()}, intent, ensure_ascii=False)
        intent.flush()
        os.fsync(intent.fileno())


def remote(source: str, *, timeout: int = 60) -> dict:
    if not SSH_HOST or not REMOTE_PYTHON or not JOBS:
        raise RuntimeError("Private worker configuration is required before SSH")
    completed = subprocess.run(["ssh", SSH_HOST, f"{REMOTE_PYTHON} -"],
                               input=source, text=True, encoding="utf-8", capture_output=True, timeout=timeout)
    if completed.returncode:
        # SSH stderr may expose addresses; retain it privately and print no raw config.
        raise RuntimeError("Approved SSH transport failed")
    return json.loads(completed.stdout)


def pending() -> list[dict]:
    result = remote(f"""import pathlib,json
root=pathlib.Path({REMOTE_BATCH!r})/'local-review-relay'
rows=[]
if root.is_dir():
 for p in sorted(root.glob('*/request.json')):
  r=json.loads(p.read_text(encoding='utf-8'))
  if not (pathlib.Path(r['directory'])/'result.json').exists(): rows.append(r)
print(json.dumps({{'requests':rows}}))
""")
    return result["requests"]


def publish(directory: Path, request: dict) -> None:
    from .local_review_relay import LocalReviewPipeline
    validator = object.__new__(LocalReviewPipeline)
    validator.review_model, validator.review_authorization_id = MODEL, AUTHORIZATION
    validator._validate_terminal(directory, request)
    process, result = read_json(directory / "process.json"), read_json(directory / "result.json")
    for name, key in (("dispatch-intent.json", "dispatch_intent_sha256"),
                      ("prelaunch-recovery.json", "prelaunch_recovery_sha256")):
        expected = process.get(key, "")
        if expected or result.get(key) or (directory / name).exists():
            if (not expected or expected != result.get(key) or sha256(directory / name) != expected):
                raise RuntimeError("Local dispatch provenance differs")
    if process.get("dispatch_intent_sha256"):
        dispatch = read_json(directory / "dispatch-intent.json")
        if (dispatch.get("request_id") != request["id"] or dispatch.get("model") != MODEL
                or dispatch.get("request_sha256") != sha256(directory / "request.json")):
            raise RuntimeError("Local dispatch intent identity differs")
    if process.get("prelaunch_recovery_sha256"):
        validate_prelaunch_recovery(directory, request, before_dispatch=False)
    files = {name: base64.b64encode((directory / name).read_bytes()).decode("ascii")
             for name in ("events.jsonl", "process.json", "result.json")}
    if (directory / "final.json").is_file():
        files["final.json"] = base64.b64encode((directory / "final.json").read_bytes()).decode("ascii")
    for name in ("dispatch-intent.json", "prelaunch-recovery.json"):
        if (directory / name).is_file():
            files[name] = base64.b64encode((directory / name).read_bytes()).decode("ascii")
    payload = base64.b64encode(json.dumps(files).encode("utf-8")).decode("ascii")
    identity = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    response = remote(f"""import pathlib,json,base64,os,tempfile,hashlib
r=json.loads(base64.b64decode({identity!r})); d=pathlib.Path(r['directory'])
actual=json.loads((d/'relay-request.json').read_text(encoding='utf-8'))
assert actual==r, 'Relay request changed'
files=json.loads(base64.b64decode({payload!r}))
for name in [n for n in files if n!='result.json']+['result.json']:
 data=base64.b64decode(files[name]); target=d/name
 if target.exists():
  assert target.read_bytes()==data, 'Existing relay evidence differs'
  continue
 fd,tmp=tempfile.mkstemp(prefix='.local-relay-',dir=d)
 with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
 os.link(tmp,target); os.unlink(tmp)
print(json.dumps({{'confirmed':True,'result_sha256':hashlib.sha256((d/'result.json').read_bytes()).hexdigest()}}))
""", timeout=180)
    if not response.get("confirmed") or response.get("result_sha256") != sha256(directory / "result.json"):
        raise RuntimeError("Local evaluation publication is unconfirmed")
    atomic_json(directory / "published.json", response)


def execute(request: dict, root: Path) -> None:
    validate_request(request, allow_expired=True)
    directory = root / request["id"]
    prelaunch_recovery = False
    if directory.exists():
        if read_json(directory / "request.json") != request:
            raise RuntimeError("Earlier local claim differs; refusing redispatch")
        if (directory / "result.json").is_file():
            publish(directory, request)
            return
        if (directory / "prelaunch-recovery.json").is_file():
            validate_prelaunch_recovery(directory, request)
            prelaunch_recovery = True
        else:
            raise RuntimeError("Earlier local claim has no terminal result; reconcile without redispatch")
    validate_request(request)
    if not prelaunch_recovery:
        directory.mkdir()
        atomic_json(directory / "request.json", request)
    bundle = directory / "workspace.tar.gz"
    if not prelaunch_recovery:
        copied = subprocess.run(["scp", f"{SSH_HOST}:{REMOTE_BATCH}/local-review-relay/{request['id']}/workspace.tar.gz", str(bundle)],
                                capture_output=True, timeout=180)
        if copied.returncode:
            raise RuntimeError("Local grading bundle transport failed")
    if bundle.stat().st_size != request["bundle_size"] or sha256(bundle) != request["bundle_sha256"]:
        raise RuntimeError("Local grading bundle is unavailable or changed")
    folder = directory / "workspace"
    if not prelaunch_recovery:
        folder.mkdir()
    extract_bundle(bundle, folder)
    authoritative = folder / request["phase"]
    for filename, key in (("schema.json", "schema_sha256"), ("prompt.txt", "prompt_sha256")):
        if sha256(authoritative / filename) != request[key]:
            raise RuntimeError("Authoritative grading material changed in transit")
        shutil.copyfile(authoritative / filename, directory / filename)
    manifests = read_json(folder / "source-manifest.json")["files"]
    for name, manifest in manifests.items():
        verify_originals(folder / name, manifest)
    transport = folder / ".atk-execution-transport.json"
    atomic_json(transport, {"ssh_host": SSH_HOST, "remote_python": REMOTE_PYTHON,
                            "workspace": request["workspace"], "request_id": request["id"]})
    prompt = (authoritative / "prompt.txt").read_text(encoding="utf-8")
    command = [shutil.which("codex"), "exec", "--skip-git-repo-check", "--json", "--color", "never",
               "--dangerously-bypass-approvals-and-sandbox", "--model", MODEL,
               "--output-schema", str(authoritative / "schema.json"), "-o", str(directory / "final.json"), "-C", str(folder)]
    for image in request["images"]:
        command += ["--image", str(folder / PurePosixPath(image).name)]
    command += ["-"]
    version = subprocess.check_output([shutil.which("codex"), "--version"], text=True, encoding="utf-8").strip()
    # A crash after this point is never eligible for prelaunch recovery, even
    # when Popen/process.json did not finish. The unknown claim remains intact.
    validate_request(request)
    reserve_dispatch(directory, request)
    with (directory / "events.jsonl").open("w", encoding="utf-8") as output:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "NO_COLOR": "1"})
        atomic_json(directory / "process.json", {"pid": proc.pid, "worker_pid": os.getpid(), "started_at": utc_now(),
                                                 "model": MODEL, "cli_version": version, "request_id": request["id"],
                                                 "execution_host": "local-windows", "workspace": str(folder), "command": command,
                                                 "dispatch_intent_sha256": sha256(directory / "dispatch-intent.json"),
                                                 "prelaunch_recovery_sha256": sha256(directory / "prelaunch-recovery.json") if prelaunch_recovery else ""})
        try:
            proc.communicate(prompt, timeout=max(1, request["deadline"] - time.time()))
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
            proc.wait(timeout=30)
            # This is a true local budget failure, not a generated product defect.
    events = []
    for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    result = {"request_id": request["id"], "model": MODEL, "authorization_id": AUTHORIZATION,
              "execution_host": "local-windows", "exit_code": proc.returncode,
              "session_id": next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), ""),
              "completed": any(e.get("type") == "turn.completed" for e in events), "finished_at": utc_now(),
              "failure_classification": classify_provider_failure(events), "schema_sha256": request["schema_sha256"],
              "prompt_sha256": request["prompt_sha256"], "events_sha256": sha256(directory / "events.jsonl"),
              "process_sha256": sha256(directory / "process.json"),
              "dispatch_intent_sha256": sha256(directory / "dispatch-intent.json"),
              "prelaunch_recovery_sha256": sha256(directory / "prelaunch-recovery.json") if prelaunch_recovery else "",
              "final_sha256": sha256(directory / "final.json") if (directory / "final.json").exists() else ""}
    for name, manifest in manifests.items():
        verify_originals(folder / name, manifest)
    atomic_json(directory / "result.json", result)
    publish(directory, request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Private operator-approved batch/SSH allowlist JSON")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("The local grading worker runs on Windows")
    configure_worker(args.config)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    import msvcrt
    with (root / ".worker.lock").open("a+b") as lock:
        lock.seek(0); lock.write(b"0"); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        atomic_json(root / "worker-process.json", {"pid": os.getpid(), "model": MODEL, "started_at": utc_now()})
        while True:
            try:
                rows = pending()
                for request in rows:
                    if request.get("deadline", 0) <= time.time():
                        claimed = root / request.get("id", "")
                        if not (claimed / "result.json").is_file():
                            continue
                    atomic_json(root / "worker-state.json", {"status": "running", "request_id": request.get("id"), "updated_at": utc_now()})
                    execute(request, root)
                atomic_json(root / "worker-state.json", {"status": "waiting_request", "updated_at": utc_now()})
            except Exception as exc:
                atomic_json(root / "worker-state.json", {"status": "needs_attention", "error_type": type(exc).__name__, "updated_at": utc_now()})
            time.sleep(10)


if __name__ == "__main__":
    main()
