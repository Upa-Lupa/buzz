"""Stage versioned common skills from explicit sources; never fetch chat-provided code."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path


def deploy(erp, runtime):
    root = Path(__file__).resolve().parent
    sources = {
        "document-reading": root / "skills/document-reading/SKILL.md",
        "order-registration": erp / "skills/order-registration/SKILL.md",
    }
    # Validate every source before replacing anything.
    contents = {name: path.read_bytes() for name, path in sources.items()}
    for name, data in contents.items():
        if (
            len(data) > 32 * 1024
            or not data.startswith(b"---\n")
            or f"name: {name}\n".encode() not in data
        ):
            raise ValueError(f"Invalid shared skill: {name}")
    manifest = {}
    for name, data in contents.items():
        target = runtime / ".agents/skills" / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stage:
            stage.write(data)
            temporary = Path(stage.name)
        temporary.replace(target)
        manifest[name] = {
            "source": str(sources[name].resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    (runtime / "skills-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    # These two small business rulesets are mandatory for this specialized
    # assistant, so include their canonical bytes in standing context as well
    # as the discoverable catalog. Do not rely on an optional model tool call.
    prompt = (root / "system-prompt.md").read_text()
    for name, data in contents.items():
        prompt += f'\n\n<deployed_shared_skill name="{name}">\n{data.decode()}\n</deployed_shared_skill>\n'
    (runtime / "system-prompt.md").write_text(prompt)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--erp", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            deploy(args.erp.resolve(), args.runtime.resolve()),
            ensure_ascii=False,
            indent=2,
        )
    )
