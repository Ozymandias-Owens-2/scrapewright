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


def test_the_navigation_is_not_worth_paying_to_read():
    """One van listing sent 110,000 characters of menu to the model, inside
    a prompt that was being truncated at the cap before it reached the
    price. Measured across four live pages, what is sent is now 46% of what
    it was."""
    menu = "".join(f'<li><a href="/c/{i}">Category {i}</a></li>' for i in range(300))
    html = (f"<html><body><nav><ul>{menu}</ul></nav>"
            '<main><h1>Van</h1><p class="price">&euro; 31.850</p></main>'
            f"<footer><ul>{menu}</ul></footer></body></html>")
    out = reduce_html(html)
    assert "Category 7" not in out
    assert "31.850" in out and 'class="price"' in out
    assert len(out) < 200


def test_the_shop_names_its_own_noise_and_we_believe_it():
    """A related-products strip is a dozen whole products with prices in the
    shop's own markup. Asked where the price lives, a model can answer with
    one of theirs -- and that selector then replays on every page, returning
    a number belonging to a different item."""
    html = ('<main><h1>Rig</h1><p class="summary-price">170,37&euro;</p></main>'
            '<section class="related products">'
            '<li><h2>Other</h2><p class="summary-price">25,00&euro;</p></li>'
            "</section>"
            '<div class="cookie-banner">We value your privacy</div>')
    out = reduce_html(html)
    assert "170,37" in out
    assert "25,00" not in out and "Other" not in out
    assert "privacy" not in out


def test_an_empty_wrapper_cannot_hold_a_price():
    html = ('<div class="spacer"><span class="icon"></span></div>'
            '<div class="price">&euro;9</div>'
            '<figure><img src="/a.jpg"></figure>')
    out = reduce_html(html)
    assert "spacer" not in out and "icon" not in out
    assert 'class="price"' in out
    assert "/a.jpg" in out          # an image is content, empty or not


def test_a_meta_tag_keeps_its_value():
    """schema.org markup in the head is often the cleanest price on the
    page; dropping the head to save characters would throw it away."""
    html = ('<head><link rel="stylesheet" href="/a.css">'
            '<meta property="product:price:amount" content="25"></head>'
            "<body><h1>Thing</h1></body>")
    out = reduce_html(html)
    assert 'content="25"' in out
    assert "a.css" not in out


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
