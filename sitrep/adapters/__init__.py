"""Every data source sitrep knows about. To add one, write an adapter and list it here."""

from .acsc import AcscAdapter
from .adsb import AdsbAdapter
from .advice import AdviceAdapter
from .aemo import AemoAdapter
from .base import SourceAdapter
from .bluesky import BlueskyAdapter
from .bom import BomAdapter
from .cloud import CloudAdapter
from .fuel import FuelAdapter
from .ioda import IodaAdapter
from .livetraffic import LiveTrafficAdapter
from .news import NewsAdapter
from .probes import ProbesAdapter
from .radar import RadarAdapter
from .ransomware import RansomwareAdapter
from .reach import ReachAdapter
from .rfs import RfsAdapter
from .telcos import TelcosAdapter
from .transit import TransitAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    cls.id: cls for cls in (
        ProbesAdapter, ReachAdapter, IodaAdapter, RadarAdapter, CloudAdapter, AcscAdapter, RansomwareAdapter,
        AdviceAdapter, RfsAdapter, BomAdapter, LiveTrafficAdapter, TransitAdapter, NewsAdapter, AemoAdapter,
        FuelAdapter, TelcosAdapter, BlueskyAdapter, AdsbAdapter,
    )
}
