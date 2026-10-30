"""Every data source sitrep knows about. To add one, write an adapter and list it here."""

from .acsc import AcscAdapter
from .aemo import AemoAdapter
from .base import SourceAdapter
from .bluesky import BlueskyAdapter
from .ioda import IodaAdapter
from .probes import ProbesAdapter
from .radar import RadarAdapter
from .telcos import TelcosAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    cls.id: cls for cls in (ProbesAdapter, IodaAdapter, RadarAdapter, AcscAdapter, AemoAdapter, TelcosAdapter, BlueskyAdapter)
}
