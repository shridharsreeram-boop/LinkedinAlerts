#!/usr/bin/env python3
"""
location_normalizer.py
-----------------------
Normalizes location strings entered by users — corrects common typos,
alternate spellings, and abbreviations to their canonical city names.

Used in sync_signups.py when processing new subscriber signups so the
corrected location is stored once and used correctly on every pipeline run.

Usage:
    from location_normalizer import normalize_locations
    corrected = normalize_locations("Banglore, Stokholm")
    # returns "Bangalore, Stockholm"
"""

# Maps common typos / alternate spellings → canonical city name
# Organized by region for easy maintenance
LOCATION_CORRECTIONS = {

    # ---- India ----
    "banglore": "Bangalore",
    "banglaore": "Bangalore",
    "bangalor": "Bangalore",
    "bangalure": "Bangalore",
    "bangaloore": "Bangalore",
    "bengaluru": "Bangalore",
    "bengalore": "Bangalore",
    "mumabi": "Mumbai",
    "mumbai": "Mumbai",
    "bombay": "Mumbai",
    "bombai": "Mumbai",
    "dlehi": "Delhi",
    "dehli": "Delhi",
    "delhy": "Delhi",
    "new dehli": "New Delhi",
    "new dlehi": "New Delhi",
    "new delhy": "New Delhi",
    "hydrabad": "Hyderabad",
    "hydrabad": "Hyderabad",
    "hyderabad": "Hyderabad",
    "hyd": "Hyderabad",
    "chenai": "Chennai",
    "chennai": "Chennai",
    "madras": "Chennai",
    "kolkatta": "Kolkata",
    "kolkata": "Kolkata",
    "calcutta": "Kolkata",
    "pune": "Pune",
    "poona": "Pune",
    "pune": "Pune",
    "ahemadabad": "Ahmedabad",
    "ahemdabad": "Ahmedabad",
    "ahmadabad": "Ahmedabad",
    "noida": "Noida",
    "gurugram": "Gurugram",
    "gurgaon": "Gurugram",
    "gurgoan": "Gurugram",

    # ---- Sweden ----
    "stokholm": "Stockholm",
    "stockhol": "Stockholm",
    "stockholme": "Stockholm",
    "stckholm": "Stockholm",
    "goteborg": "Gothenburg",
    "göteborg": "Gothenburg",
    "gothenberg": "Gothenburg",
    "gothenburg": "Gothenburg",
    "gothenborg": "Gothenburg",
    "gbg": "Gothenburg",
    "malmo": "Malmö",
    "malmö": "Malmö",
    "malmoe": "Malmö",
    "karlstd": "Karlstad",
    "karlstad": "Karlstad",
    "upsala": "Uppsala",
    "uppsala": "Uppsala",
    "linkoping": "Linköping",
    "linköping": "Linköping",
    "linkøping": "Linköping",
    "orebro": "Örebro",
    "örebro": "Örebro",
    "vasteras": "Västerås",
    "västerås": "Västerås",
    "norrkoping": "Norrköping",
    "norrköping": "Norrköping",
    "jonkoping": "Jönköping",
    "jönköping": "Jönköping",
    "umea": "Umeå",
    "umeå": "Umeå",
    "gavle": "Gävle",
    "gävle": "Gävle",
    "boras": "Borås",
    "borås": "Borås",
    "sundsvall": "Sundsvall",
    "eskilstuna": "Eskilstuna",
    "halmstad": "Halmstad",
    "vaxjo": "Växjö",
    "växjö": "Växjö",
    "trollhattan": "Trollhättan",
    "trollhättan": "Trollhättan",
    "lund": "Lund",
    "helsingborg": "Helsingborg",

    # ---- UK ----
    "londen": "London",
    "londan": "London",
    "londun": "London",
    "london": "London",
    "manchestr": "Manchester",
    "manchaster": "Manchester",
    "manchester": "Manchester",
    "birminghm": "Birmingham",
    "birmingam": "Birmingham",
    "birmingham": "Birmingham",
    "ednburgh": "Edinburgh",
    "edinburgh": "Edinburgh",
    "glasgw": "Glasgow",
    "glasgow": "Glasgow",
    "leeds": "Leeds",
    "bristl": "Bristol",
    "bristol": "Bristol",

    # ---- Germany ----
    "berlin": "Berlin",
    "berln": "Berlin",
    "belin": "Berlin",
    "munich": "Munich",
    "munchen": "Munich",
    "münchen": "Munich",
    "hambrug": "Hamburg",
    "hamburg": "Hamburg",
    "frankfort": "Frankfurt",
    "franfurt": "Frankfurt",
    "frankfurt": "Frankfurt",
    "cologne": "Cologne",
    "koln": "Cologne",
    "köln": "Cologne",
    "stutgart": "Stuttgart",
    "stuttgart": "Stuttgart",

    # ---- USA ----
    "newyork": "New York",
    "new york": "New York",
    "new yok": "New York",
    "nyc": "New York",
    "ny": "New York",
    "san fransisco": "San Francisco",
    "san francisco": "San Francisco",
    "sf": "San Francisco",
    "los angelos": "Los Angeles",
    "los angeles": "Los Angeles",
    "la": "Los Angeles",
    "chcago": "Chicago",
    "chicago": "Chicago",
    "seatle": "Seattle",
    "seattle": "Seattle",
    "autin": "Austin",
    "austin": "Austin",
    "bostton": "Boston",
    "boston": "Boston",

    # ---- Canada ----
    "torronto": "Toronto",
    "toronto": "Toronto",
    "vancover": "Vancouver",
    "vancouver": "Vancouver",
    "monteal": "Montreal",
    "montreal": "Montreal",
    "calgry": "Calgary",
    "calgary": "Calgary",

    # ---- Australia ----
    "sydny": "Sydney",
    "sydney": "Sydney",
    "melbourn": "Melbourne",
    "melbourne": "Melbourne",
    "brisban": "Brisbane",
    "brisbane": "Brisbane",
    "perht": "Perth",
    "perth": "Perth",

    # ---- Netherlands ----
    "amstredam": "Amsterdam",
    "amsterdam": "Amsterdam",
    "roterdam": "Rotterdam",
    "rotterdam": "Rotterdam",

    # ---- France ----
    "paries": "Paris",
    "paris": "Paris",
    "lyon": "Lyon",
    "marsielle": "Marseille",
    "marseille": "Marseille",

    # ---- Singapore ----
    "singapur": "Singapore",
    "singapor": "Singapore",
    "singapore": "Singapore",

    # ---- UAE ----
    "dubai": "Dubai",
    "dubay": "Dubai",
    "abudhabi": "Abu Dhabi",
    "abu dhabi": "Abu Dhabi",

    # ---- Generic / remote ----
    "remotee": "Remote",
    "remte": "Remote",
    "remote": "Remote",
    "wfh": "Remote",
    "work from home": "Remote",
    "anywhere": "Remote",
}


def normalize_location(location: str) -> str:
    """Normalize a single location string — correct typos and
    return the canonical city name. Preserves case if no match found."""
    key = location.strip().lower()
    return LOCATION_CORRECTIONS.get(key, location.strip().title())


def normalize_locations(location_str: str) -> str:
    """Normalize a comma-separated list of locations.
    Returns a comma-separated string of corrected city names.

    Example:
        normalize_locations("Banglore, Stokholm, remote")
        → "Bangalore, Stockholm, Remote"
    """
    if not location_str or not location_str.strip():
        return location_str

    parts = [loc.strip() for loc in location_str.split(",") if loc.strip()]
    corrected = [normalize_location(p) for p in parts]
    return ", ".join(corrected)


if __name__ == "__main__":
    # Quick self-test
    tests = [
        ("Banglore", "Bangalore"),
        ("Stokholm", "Stockholm"),
        ("san fransisco", "San Francisco"),
        ("göteborg", "Gothenburg"),
        ("mumbai", "Mumbai"),
        ("Bangalore, Chennai, Stokholm", "Bangalore, Chennai, Stockholm"),
        ("remote", "Remote"),
        ("UnknownCity", "Unknowncity"),  # unknown → title case passthrough
    ]
    print("Running self-tests...")
    all_passed = True
    for inp, expected in tests:
        result = normalize_locations(inp)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_passed = False
        print(f"  {status} normalize_locations({inp!r}) → {result!r} (expected {expected!r})")
    print("All tests passed!" if all_passed else "Some tests failed.")
