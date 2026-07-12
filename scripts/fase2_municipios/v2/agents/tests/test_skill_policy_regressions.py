from pathlib import Path

import pytest


pytestmark = pytest.mark.offline


def test_feed_tag_is_not_rejected_merely_for_news_cards() -> None:
    text = Path("skills/fase2-resource-certifier/SKILL.md").read_text(encoding="utf-8")
    assert "LER MAIS não transforma o AGREGADOR em notícia individual" in text
