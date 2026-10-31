# Data files shipped with sitrep

## places_nsw.csv

Every NSW suburb and locality with a point, used to find place names in headlines and
posts (see `sitrep/places.py`). Made by `tools/build_places.py` from:

> Suburbs and Localities 2026 (Australian Statistical Geography Standard, Edition 4)
> and Greater Capital City Statistical Areas 2026 (Greater Sydney),
> © Commonwealth of Australia (Australian Bureau of Statistics),
> licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Changes made: points taken from the ABS's own point layer, names cleaned (the state
suffix the ABS adds, as in "Richmond (NSW)", moved into the `elsewhere` column), and each
point marked as inside or outside Greater Sydney.

To refresh it: `python tools/build_places.py`.
