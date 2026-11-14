"""constraints.txt pins every package the release build installs."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_pins(name: str) -> dict[str, str]:
    pins = {}
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            package, version = line.split("==")
            pins[package.lower().replace("_", "-")] = version
    return pins


def test_constraints_agree_with_requirements():
    """兩邊版本不一致時 pip 會直接失敗，打包流程跟著中止。"""
    requirements = read_pins("requirements.txt")
    constraints = read_pins("constraints.txt")
    assert {name: constraints.get(name) for name in requirements} == requirements
