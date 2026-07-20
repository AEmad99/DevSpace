"""Regression guards for the welcome-screen usability pass."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MODELS = (ROOT / "static" / "js" / "models.js").read_text(encoding="utf-8")
WELCOME = (ROOT / "static" / "js" / "welcomeScreen.js").read_text(encoding="utf-8")
THEME = (ROOT / "static" / "js" / "theme.js").read_text(encoding="utf-8")


def test_welcome_primary_and_quick_actions_are_semantic_buttons():
    assert '<button type="button" class="welcome-primary setup-trigger-link"' in INDEX
    assert INDEX.count('class="welcome-launch" data-welcome-target=') == 3
    assert 'aria-labelledby="welcome-title"' in INDEX


def test_welcome_model_state_has_one_owner():
    assert "import { setWelcomeModelState } from './welcomeScreen.js';" in MODELS
    assert "setWelcomeModelState(false)" in MODELS
    assert "setWelcomeModelState(true)" in MODELS
    assert "screen.classList.toggle('welcome-configured', hasModels)" in WELCOME


def test_preset_themes_are_keyboard_operable():
    assert '<button type="button" class="theme-swatch' in THEME
    assert 'aria-pressed="${name === activeName ? \'true\' : \'false\'}"' in THEME
    assert "sw.setAttribute('aria-pressed', 'true')" in THEME

