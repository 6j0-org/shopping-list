"""Guess which grocery-store section an item lives in, from its name.

Matching is keyword based. Every keyword that appears in the item name (as a
whole word, optionally pluralised) is a candidate; the longest keyword wins, so
"peanut butter" beats "butter", "baking soda" beats "soda", and "ice cream"
beats "cream".
"""

import re

PANTRY = "Pantry"
OTHER = "Other"

# Listed in a typical walk through a store; this order is the sort order.
SECTION_KEYWORDS = {
    "Produce": (
        "apple, apricot, avocado, banana, basil, beet, berry, blackberry, "
        "blueberry, bok choy, broccoli, brussels sprout, cabbage, cantaloupe, "
        "carrot, cauliflower, celery, cherry, chard, cilantro, clementine, "
        "collard, corn on the cob, cucumber, dill, eggplant, fennel, fig, "
        "garlic, ginger, grape, grapefruit, green bean, green onion, herb, "
        "honeydew, jalapeno, kale, kiwi, leek, lemon, lettuce, lime, mandarin, "
        "mango, melon, mint, mushroom, nectarine, okra, onion, orange, parsley, "
        "parsnip, peach, pear, pepper, bell pepper, persimmon, pineapple, plum, "
        "pomegranate, potato, pumpkin, radish, raspberry, rhubarb, romaine, "
        "rosemary, salad mix, scallion, shallot, snap pea, spinach, sprout, "
        "squash, strawberry, sweet potato, tangerine, thyme, tomato, turnip, "
        "watermelon, yam, zucchini, arugula, asparagus, artichoke, tomatillo, "
        "poblano, serrano, habanero, chili pepper, sage, fresh "
    ),
    "Bakery": (
        "bagel, baguette, bread, brioche, bun, cake, ciabatta, cookie, "
        "croissant, cupcake, danish, donut, doughnut, english muffin, focaccia, "
        "hamburger bun, hot dog bun, loaf, muffin, naan, pastry, pie, pita, "
        "roll, sourdough, tortilla, dinner roll "
    ),
    "Deli": (
        "deli, hummus, prosciutto, salami, pastrami, bologna, sliced turkey, "
        "deli meat, lunch meat, rotisserie chicken, pepperoni, olive bar, "
        "prepared salad "
    ),
    "Meat & Seafood": (
        "bacon, beef, brisket, chicken, chicken breast, chicken thigh, clam, "
        "cod, crab, duck, fish, ground beef, ground turkey, halibut, ham, hot "
        "dog, lamb, lobster, mahi, meat, meatball, mussel, oyster, pork, pork "
        "chop, rib, roast, salmon, sausage, scallop, shrimp, steak, tilapia, "
        "trout, tuna steak, turkey, veal, venison, bratwurst, chorizo "
    ),
    "Dairy & Eggs": (
        "butter, buttermilk, cheddar, cheese, cottage cheese, cream, cream "
        "cheese, creamer, egg, feta, gouda, half and half, heavy cream, kefir, "
        "margarine, milk, mozzarella, oat milk, almond milk, parmesan, "
        "provolone, ricotta, sour cream, string cheese, swiss, whipped cream, "
        "yogurt, yoghurt, ghee, brie, goat cheese "
    ),
    "Frozen": (
        "frozen, ice, ice cream, popsicle, frozen pizza, tv dinner, waffle, "
        "sorbet, gelato, fish stick, tater tot, hash brown, ice pop "
    ),
    "Breakfast & Cereal": (
        "cereal, granola, oatmeal, oat, pancake mix, syrup, maple syrup, pop "
        "tart, breakfast bar, granola bar, grits, cream of wheat, muesli "
    ),
    "Canned & Dry Goods": (
        "bean, black bean, broth, canned, chickpea, couscous, kidney bean, "
        "lentil, macaroni, noodle, pasta, penne, quinoa, ramen, rice, soup, "
        "spaghetti, stock, tomato sauce, tomato paste, diced tomato, canned "
        "tomato, canned tuna, tuna, pinto bean, refried bean, barley, farro, "
        "lasagna, orzo, fettuccine, linguine, rigatoni, bouillon, mac and "
        "cheese "
    ),
    "Baking & Spices": (
        "baking powder, baking soda, brown sugar, chocolate chip, cinnamon, "
        "cocoa, cornmeal, cornstarch, extract, flour, nutmeg, oregano, paprika, "
        "powdered sugar, salt, black pepper, spice, sugar, vanilla, yeast, "
        "cumin, chili powder, garlic powder, onion powder, bay leaf, cake mix, "
        "frosting, sprinkles, shortening, molasses, gelatin, food coloring, "
        "cardamom, clove, turmeric, curry powder, seasoning "
    ),
    "Condiments & Sauces": (
        "bbq sauce, barbecue sauce, dressing, hot sauce, honey, jam, jelly, "
        "ketchup, mayo, mayonnaise, mustard, nutella, oil, olive oil, peanut "
        "butter, pickle, relish, salsa, soy sauce, sriracha, vinegar, "
        "worcestershire, teriyaki, pesto, marinara, pasta sauce, salad "
        "dressing, ranch, tahini, almond butter, vegetable oil, canola oil, "
        "coconut oil, sesame oil, fish sauce, hoisin, spread, olive "
    ),
    "Snacks": (
        "chip, cracker, candy, chocolate, gum, jerky, nut, peanut, almond, "
        "cashew, pistachio, popcorn, pretzel, trail mix, tortilla chip, dried "
        "fruit, raisin, rice cake, fruit snack, goldfish "
    ),
    "Beverages": (
        "coffee, juice, kombucha, lemonade, soda, seltzer, sparkling water, "
        "tea, water, energy drink, sports drink, gatorade, coke, pepsi, sprite, "
        "orange juice, apple juice, coconut water, drink mix, cold brew, "
        "espresso "
    ),
    "Beer, Wine & Spirits": (
        "beer, wine, vodka, whiskey, whisky, rum, gin, tequila, bourbon, "
        "champagne, prosecco, cider, hard seltzer, liquor, sake, mezcal, "
        "brandy, vermouth "
    ),
    "Household & Cleaning": (
        "aluminum foil, bleach, battery, detergent, dish soap, dishwasher, "
        "foil, garbage bag, trash bag, laundry, light bulb, napkin, paper "
        "towel, plastic wrap, sponge, toilet paper, ziploc, zip bag, cleaner, "
        "disinfectant, fabric softener, dryer sheet, tissue, candle, match, "
        "lighter, parchment paper, wax paper, air freshener "
    ),
    "Personal Care": (
        "body wash, conditioner, cotton ball, deodorant, floss, lotion, "
        "mouthwash, razor, shampoo, shaving cream, soap, sunscreen, toothbrush, "
        "toothpaste, tampon, lip balm, hand soap, moisturizer, q-tip, makeup, "
        "hair gel "
    ),
    "Health & Pharmacy": (
        "advil, allergy, antacid, aspirin, band-aid, bandage, cold medicine, "
        "cough drop, ibuprofen, medicine, multivitamin, pain reliever, tylenol, "
        "vitamin, acetaminophen, melatonin, first aid, thermometer "
    ),
    "Baby": (
        "baby, diaper, formula, baby food, baby wipe, wipe, pacifier "
    ),
    "Pet": (
        "cat food, dog food, cat litter, kitty litter, litter, pet food, dog "
        "treat, cat treat, bird seed, fish food "
    ),
}

SECTIONS = list(SECTION_KEYWORDS) + [OTHER]
ALL_SECTIONS = SECTIONS + [PANTRY]
SECTION_ORDER = {name: i for i, name in enumerate(ALL_SECTIONS)}


def _keywords(text):
    return {k.strip() for k in text.split(",") if k.strip()}


_INDEX = []  # (keyword, section, compiled regex)
for _section, _blob in SECTION_KEYWORDS.items():
    for _kw in _keywords(_blob):
        pattern = r"(?<![a-z])" + re.escape(_kw) + r"(?:s|es)?(?![a-z])"
        _INDEX.append((_kw, _section, re.compile(pattern)))

# "berries", "cherries" -> "berry", "cherry"
_IES = re.compile(r"ies\b")


def classify(name):
    """Return the store section an item name most likely belongs to."""
    text = name.lower().strip()
    text = _IES.sub("y", text)
    text = re.sub(r"[^a-z0-9&' -]+", " ", text)
    best = None
    for kw, section, rx in _INDEX:
        if rx.search(text) and (best is None or len(kw) > len(best[0])):
            best = (kw, section)
    return best[1] if best else OTHER


# Open Food Facts category tags -> section, used as a hint for scanned items.
_OFF_HINTS = [
    ("en:frozen", "Frozen"),
    ("en:beverages", "Beverages"),
    ("en:alcoholic", "Beer, Wine & Spirits"),
    ("en:dairies", "Dairy & Eggs"),
    ("en:cheeses", "Dairy & Eggs"),
    ("en:eggs", "Dairy & Eggs"),
    ("en:meats", "Meat & Seafood"),
    ("en:seafood", "Meat & Seafood"),
    ("en:breakfast-cereals", "Breakfast & Cereal"),
    ("en:breads", "Bakery"),
    ("en:snacks", "Snacks"),
    ("en:spreads", "Condiments & Sauces"),
    ("en:condiments", "Condiments & Sauces"),
    ("en:sauces", "Condiments & Sauces"),
    ("en:canned-foods", "Canned & Dry Goods"),
    ("en:pastas", "Canned & Dry Goods"),
    ("en:rices", "Canned & Dry Goods"),
    ("en:soups", "Canned & Dry Goods"),
    ("en:legumes", "Canned & Dry Goods"),
    ("en:baking", "Baking & Spices"),
    ("en:spices", "Baking & Spices"),
    ("en:fruits", "Produce"),
    ("en:vegetables", "Produce"),
]


def classify_from_tags(tags):
    for prefix, section in _OFF_HINTS:
        if any(t.startswith(prefix) for t in tags):
            return section
    return None
