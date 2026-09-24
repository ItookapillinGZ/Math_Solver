from __future__ import annotations

from pathlib import Path


class SkillCatalog:
    """Deterministic on-disk catalog for SKILL.md manifests."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.registry: dict[str, dict] = {}
        self.scan()

    @staticmethod
    def parse_frontmatter(text: str) -> tuple[dict, str]:
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        meta: dict[str, str] = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip().strip('"').strip("'")
        return meta, parts[2].strip()

    def scan(self) -> None:
        self.registry.clear()
        if not self.root.exists():
            return
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                continue
            raw = manifest.read_text()
            meta, _ = self.parse_frontmatter(raw)
            name = meta.get("name", directory.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            self.registry[name] = {
                "name": name,
                "description": desc,
                "content": raw,
            }

    def list_text(self) -> str:
        if not self.registry:
            return "(no skills found)"
        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.registry.values()
        )

    def load(self, name: str) -> str:
        skill = self.registry.get(name)
        if not skill:
            available = ", ".join(self.registry.keys()) or "(none)"
            return f"Skill not found: {name}. Available: {available}"
        return skill["content"]
