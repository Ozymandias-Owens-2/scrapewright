"""The recipe cache must survive the volume it lives on.

Litestream replicates the SQLite database to Tigris and restores it on boot.
It replicates SQLite and nothing else, and the recipes are a JSON file beside
it on the same disk: 58 of them on the live machine, no copy anywhere, 300
credits each to work out again.
"""
import json

from scrapewright.service import recipe_backup
from scrapewright.service.store import Store

RECIPES = {"shop.test": {"fields": {"price": ".p"}, "compiled_at": 1.0}}


def test_a_lost_volume_does_not_lose_the_recipes(tmp_path):
    store = Store(tmp_path / "s.db")
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(RECIPES), encoding="utf-8")

    assert recipe_backup.back_up(store, path) is True

    path.unlink()                                   # the volume goes away
    assert recipe_backup.restore_if_missing(store, path) is True
    assert json.loads(path.read_text(encoding="utf-8")) == RECIPES


def test_a_machine_that_still_has_its_file_keeps_it(tmp_path):
    """The live file may hold compilations newer than the last snapshot, so
    the restore fills a gap and never overwrites."""
    store = Store(tmp_path / "s.db")
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(RECIPES), encoding="utf-8")
    recipe_backup.back_up(store, path)

    newer = {**RECIPES, "later.test": {"fields": {"price": ".x"}}}
    path.write_text(json.dumps(newer), encoding="utf-8")

    assert recipe_backup.restore_if_missing(store, path) is False
    assert json.loads(path.read_text(encoding="utf-8")) == newer


def test_rubbish_is_never_backed_up_or_restored_over(tmp_path):
    """A torn write leaves invalid JSON, which RecipeCache reads as an empty
    cache. Saving that would replace a good backup with nothing -- which is
    the failure this exists to prevent, arriving by the other door."""
    store = Store(tmp_path / "s.db")
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(RECIPES), encoding="utf-8")
    recipe_backup.back_up(store, path)

    path.write_text('{"shop.test": {"fiel', encoding="utf-8")    # cut short
    assert recipe_backup.back_up(store, path) is False
    assert json.loads(store.load_recipes()) == RECIPES

    # and a truncated file counts as missing, so the good copy comes back
    assert recipe_backup.restore_if_missing(store, path) is True
    assert json.loads(path.read_text(encoding="utf-8")) == RECIPES


def test_an_unchanged_file_is_not_rewritten_every_minute(tmp_path):
    store = Store(tmp_path / "s.db")
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(RECIPES), encoding="utf-8")

    assert recipe_backup.back_up(store, path) is True
    assert recipe_backup.back_up(store, path) is False


def test_an_empty_cache_is_not_worth_saving(tmp_path):
    store = Store(tmp_path / "s.db")
    path = tmp_path / "recipes.json"
    path.write_text("{}", encoding="utf-8")
    assert recipe_backup.back_up(store, path) is False
    assert store.load_recipes() is None
