#!/usr/bin/env python3
"""Install only the bundled Skill into an existing skills directory; preview by default."""
import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "dot-wechat-bridge"


def install(skills_dir, apply=False):
    source = ROOT / "skills" / NAME
    base = Path(os.path.abspath(skills_dir))
    if any(p.is_symlink() for p in (base, *base.parents)) or not base.is_dir():
        raise ValueError("skills_dir_must_be_existing_real_directory")
    target = base / NAME
    if target.exists() or target.is_symlink():
        raise ValueError("existing_skill_not_overwritten")
    files = []
    for p in sorted(source.rglob("*")):
        if p.is_symlink() or (not p.is_dir() and not p.is_file()):
            raise ValueError("unsafe_skill_source")
        if p.is_file():
            files.append((p.relative_to(source), p.read_bytes()))
    if not any(str(rel) == "SKILL.md" for rel, _ in files):
        raise ValueError("missing_skill_entrypoint")
    if apply:
        # Exclusive mkdir refuses an existing target, including a concurrent install.
        target.mkdir()
        for rel, data in files:
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("xb") as handle:
                handle.write(data)
            if out.read_bytes() != data:
                raise OSError("copy_verification_failed")
    return {"applied": apply, "skill": NAME, "files": [
        {"path": rel.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for rel, data in files]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.skills_dir, args.apply), sort_keys=True))
    except (OSError, ValueError) as error:
        # I/O failures may leave a new partial directory; never overwrite or silently remove it.
        print(json.dumps({"completed": False, "error": type(error).__name__,
                          "detail": str(error) if isinstance(error, ValueError) else "io_failure_inspect_target"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
