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


class GeneratedRecipeTest(unittest.TestCase):
    def test_ingredient_lines(self):
        cases = {
            "1 (8 oz.) pkg. cream cheese, softened": ("1 (8 oz.) pkg.", "cream cheese", "softened"),
            "1 1/2 lb. ground beef": ("1 1/2 lb.", "ground beef", ""),
            "2 to 3 Tbsp. butter, melted": ("2 to 3 Tbsp.", "butter", "melted"),
            "½ tsp. vanilla": ("½ tsp.", "vanilla", ""),
            "3 large eggs": ("3", "large eggs", ""),
            "salt and pepper to taste": ("", "salt and pepper", "to taste"),
            "1 c. chopped nuts (optional)": ("1 c.", "chopped nuts", "optional"),
            "can of tuna": ("", "can of tuna", ""),
            # RecipeNLG lost its slashes and apostrophes
            "12 cup shredded cheddar cheese": ("1/2 cup", "shredded cheddar cheese", ""),
            "1 12 c. milk": ("1 1/2 c.", "milk", ""),
            "12 oz. can tomato paste": ("12 oz. can", "tomato paste", ""),
            "4 5 medium potatoes": ("4–5", "medium potatoes", ""),
            "1 tsp mccormick s montreal steak seasoning": ("1 tsp", "mccormick's montreal steak seasoning", ""),
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                got = recipes.parse_ingredient(line)
                self.assertEqual((got["quantity"], got["name"], got["note"]), want)


class GeneratorServiceTest(unittest.TestCase):
    """recipegen/server.py's parts that don't need the model (or PyTorch)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "recipegen_server", Path(__file__).resolve().parent.parent / "recipegen" / "server.py")
        cls.gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gen)

    def test_reads_the_models_sections(self):
        text = ("<pad> title: macaroni and corn <section> ingredients: 2 c. macaroni <sep> 1 can cream corn <section> "
                "directions: cook macaroni. <sep> stir in corn.</s><pad>")
        self.assertEqual(self.gen.parse(text), {"title": "macaroni and corn",
                                                "ingredients": ["2 c. macaroni", "1 can cream corn"],
                                                "directions": ["cook macaroni.", "stir in corn."]})

    def test_checks_requests(self):
        self.assertEqual(self.gen._clean_request({"ingredients": [" rice ", "egg"], "count": 2}), (["rice", "egg"], 2))
        for bad in ([], {"ingredients": []}, {"ingredients": [1]}, {"ingredients": ["x"] * 16},
                    {"ingredients": ["x"], "count": 4}, {"ingredients": [" "]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.gen._clean_request(bad)


class GeneratedDraftTest(unittest.TestCase):
    def test_titles_and_steps_get_their_apostrophes_back(self):
        draft = recipes.draft_from_generated({"title": "johnny s shepherd s pie", "ingredients": ["1 lb ground beef"],
                                              "directions": ["brown the beef in grandma s skillet."]})
        self.assertEqual((draft["title"], draft["steps"]), ("Johnny's shepherd's pie", ["Brown the beef in grandma's skillet."]))
