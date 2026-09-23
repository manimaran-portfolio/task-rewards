import _pathsetup  # noqa: F401

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicationContractTests(unittest.TestCase):
    def test_skill_uses_supported_directory_template(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("${HERMES_SKILL_DIR}/scripts/rewards.py", skill)
        self.assertNotIn("${SKILL_DIR}", skill)

    def test_skill_declares_optional_todoist_secret(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("required_environment_variables:", skill)
        self.assertIn("name: TODOIST_API_TOKEN", skill)
        self.assertNotIn("profiles/*/.env", skill)

    def test_skill_references_every_file_needed_by_direct_url_install(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        required = {
            "scripts/rewards.py",
            "scripts/wizard.py",
            "scripts/profile_paths.py",
            "scripts/backends/__init__.py",
            "scripts/backends/todoist.py",
            "scripts/backends/markdown.py",
            "scripts/backends/json.py",
            "templates/config.example.json",
        }
        for relative_path in required:
            with self.subTest(relative_path=relative_path):
                self.assertIn(relative_path, skill)

    def test_readme_clone_instructions_are_copy_pasteable(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "git clone https://github.com/manimaran-portfolio/task-rewards.git",
            readme,
        )
        self.assertIn("cd ~/.hermes/skills/productivity/task-rewards", readme)


if __name__ == "__main__":
    unittest.main()