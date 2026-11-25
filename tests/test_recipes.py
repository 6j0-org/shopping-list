"""Reading recipe pages (recipes.read_page) without the network or Claude."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recipes  # noqa: E402


class ReadPageTest(unittest.TestCase):
    def test_recipe_data_wins_and_loses_its_photos(self):
        data = {"@context": "https://schema.org", "@type": "Recipe", "name": "Soup",
                "image": ["https://example.com/soup.jpg"], "aggregateRating": {"ratingValue": 5},
                "recipeIngredient": ["1 onion"], "recipeInstructions": [{"@type": "HowToStep", "text": "Boil."}]}
        page = (f'<title>Soup!</title><script type="application/ld+json">not json</script>'
                f'<script type="application/ld+json">[{{"@type": "Organization"}}, {json.dumps(data)}]</script>'
                f'<p>A long story about soup.</p>')
        out = recipes.read_page(page)
        self.assertIn("Page title: Soup!", out)
        self.assertIn('"recipeIngredient": ["1 onion"]', out)
        self.assertIn("Boil.", out)
        for gone in ("soup.jpg", "ratingValue", "long story"):
            self.assertNotIn(gone, out)

    def test_text_without_scripts_navigation_or_images(self):
        page = """<html><head><title>Bread</title><style>p { color: red }</style></head><body>
            <nav>Home | Recipes</nav><h1>Bread</h1><img src="bread.jpg" alt="a loaf">
            <ul><li>3 cups flour</li><li>1 tsp   yeast</li></ul><script>track()</script>
            <p>Knead &amp; bake.</p><footer>© Site</footer></body></html>"""
        out = recipes.read_page(page)
        self.assertEqual(out, "Page title: Bread\n\nThe page's text:\nBread\n3 cups flour\n1 tsp yeast\nKnead & bake.")


class MatchTest(unittest.TestCase):
    def test_filter_and_order(self):
        book = [
            {"id": 1, "title": "b", "ingredients": [{"name": "Eggs"}, {"name": "Milk"}]},
            {"id": 2, "title": "a", "ingredients": [{"name": "Eggs"}, {"name": "Water"}]},
            {"id": 3, "title": "c", "ingredients": [{"name": "Saffron"}]},
        ]
        got = recipes.match(book, ["Free Range Eggs"])
        self.assertEqual([(r["id"], r["have"], r["missing"]) for r in got], [(2, 1, 0), (1, 1, 1), (3, 0, 1)])
        self.assertEqual([r["id"] for r in recipes.match(book, ["Eggs"], max_missing=0)], [2])


if __name__ == "__main__":
    unittest.main()

