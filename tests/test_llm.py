from scrapewright.extract.llm import LlmExtractor, recipe_from_text

MODEL_REPLY = """Here is the extractor:
```json
{
  "title": ".product__title",
  "price": ".price",
  "brand": ".brand",
  "images": ".gallery__img",
  "description": ".description",
  "sku": ".sku-code",
  "modes": {"images": "attr:src"}
}
```
"""


def test_recipe_from_text_tolerates_fences_and_prose():
    recipe = recipe_from_text(MODEL_REPLY, origin="test")
    assert recipe is not None
    assert recipe.title == ".product__title"
    assert recipe.mode_for("images") == "attr:src"
    assert recipe.mode_for("title") == "text"      # default
    assert recipe.origin == "test"


def test_recipe_from_text_rejects_garbage():
    assert recipe_from_text("no json here") is None
    assert recipe_from_text('{"modes": {}}') is None   # no real fields


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessages:
    def __init__(self, reply):
        self._reply = reply
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return type("Msg", (), {"content": [_FakeBlock(self._reply)]})()


class _FakeClient:
    def __init__(self, reply):
        self.messages = _FakeMessages(reply)


def test_synthesize_uses_injected_client():
    client = _FakeClient(MODEL_REPLY)
    ex = LlmExtractor(client=client)
    recipe = ex.synthesize("<html>...</html>", "https://x/p")
    assert recipe is not None
    assert recipe.price == ".price"
    assert client.messages.calls == 1
