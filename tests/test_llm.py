from scrapewright.extract.llm import LlmExtractor, recipe_from_text, reduce_html

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


def test_reduce_html_drops_noise_but_keeps_structure():
    srcset = "/img-1200.jpg 1200w, " * 200
    html = (
        "<html><head><style>.x{color:red}</style>"
        "<script>var a=1</script></head>"
        "<body><!-- build 42 -->"
        f'<img class="hero" src="/a.jpg" srcset="{srcset}">'
        '<div class="price">€1290</div>'
        "</body></html>"
    )
    out = reduce_html(html)
    assert "var a=1" not in out and ".x{color:red}" not in out
    assert "build 42" not in out
    assert 'class="hero"' in out and 'class="price"' in out   # selectors survive
    assert "€1290" in out
    assert len(out) < 1500                                    # srcset bulk gone


def test_reduce_html_reaches_content_deep_in_a_large_page():
    """Regression: a 90k-char rendered page hid the price past the old 14k cap,
    so synthesis produced a recipe with price=null."""
    filler = "<div class='row'>lorem ipsum dolor sit amet</div>" * 2000
    html = f"<html><body>{filler}<div class='price'>€1600</div></body></html>"
    assert len(html) > 80_000
    out = reduce_html(html)
    assert "€1600" in out


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
