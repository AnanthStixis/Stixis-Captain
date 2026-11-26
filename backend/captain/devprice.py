"""Set the price Captain uses for a model alias (no API key involved):

    python -m captain.devprice ALIAS INPUT OUTPUT [CACHED]   (dollars per million tokens)

Prices drive budget caps and the cost figures in the dashboard; they do not change what the
provider bills. Take them from your provider's pricing page. A new price is a new version
that applies from now on; earlier calls keep the price they were costed with.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from captain.config import build_from_env
from captain.db import schema as s
from captain.db.stores import save_price
from captain.devrealprovider import per_million
from captain.devseed import infra_engine


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    alias = argv[0]
    try:
        in_m, out_m = per_million(argv[1]), per_million(argv[2])
        cached = per_million(argv[3]) if len(argv) == 4 else None
    except ValueError as e:
        print(e)
        return 2
    built, _ = build_from_env()
    reg = built.core.m.registry
    model = reg.usable_model(alias) if reg else None
    if model is None:
        print(f"'{alias}' is not a registered, enabled model")
        return 1
    from captain.core.usage.pricing import ModelPrice

    engine = infra_engine()
    now = datetime.now(UTC)
    with engine.connect() as c:
        old = c.execute(
            select(s.model_prices.c.input_per_m, s.model_prices.c.output_per_m)
            .where(s.model_prices.c.model == alias)
            .order_by(s.model_prices.c.effective_from.desc())
            .limit(1)
        ).first()
    save_price(
        engine,
        ModelPrice(
            alias, model.provider_id, "dev-" + now.strftime("%Y%m%d%H%M%S"),
            now - timedelta(minutes=1), in_m, out_m, cached_input_per_m=cached,
        ),
    )  # fmt: skip
    was = f"{old[0] / 1e6:g} / {old[1] / 1e6:g}" if old else "none"
    print(f"{alias}: price per million tokens was {was} (input / output), now "
          f"{in_m / 1e6:g} / {out_m / 1e6:g}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
