"""Pack registry: pack ID -> PackDefinition.

The registry is the single source of pack availability. The runner only
executes packs present here; anything else fails closed. Each pack is
built by a `build_<id>_pack()` factory so imports stay lazy and the
offline FA-00 pack never depends on Tiger/MCP runtime imports.
"""

from __future__ import annotations

from tradehub.acceptance.runner import PackDefinition


def _build_all() -> dict[str, PackDefinition]:
    from tradehub.acceptance.packs.fa00 import build_fa00_pack
    from tradehub.acceptance.packs.fa01 import build_fa01_pack
    from tradehub.acceptance.packs.fa02 import build_fa02_pack
    from tradehub.acceptance.packs.fa03 import build_fa03_pack
    from tradehub.acceptance.packs.fa04 import build_fa04_pack
    from tradehub.acceptance.packs.fa05 import build_fa05_pack
    from tradehub.acceptance.packs.fa08 import build_fa08_pack

    packs = [
        build_fa00_pack(),
        build_fa01_pack(),
        build_fa02_pack(),
        build_fa03_pack(),
        build_fa04_pack(),
        build_fa05_pack(),
        build_fa08_pack(),
    ]
    return {pack.pack_id: pack for pack in packs}


PACKS = _build_all()
