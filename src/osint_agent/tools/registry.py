"""Tool registry — discovers, configures, and provides access to all adapters."""

from osint_agent.tools.base import ToolAdapter
from osint_agent.tools.maigret import MaigretAdapter
from osint_agent.tools.holehe_adapter import HoleheAdapter
from osint_agent.tools.edgar import EdgarAdapter
from osint_agent.tools.courtlistener import CourtListenerAdapter
from osint_agent.tools.openfec import OpenFECAdapter
from osint_agent.tools.wayback import WaybackAdapter
from osint_agent.tools.exiftool import ExifToolAdapter
from osint_agent.tools.phoneinfoga import PhoneInfogaAdapter
from osint_agent.tools.theharvester import TheHarvesterAdapter
from osint_agent.tools.ddg_search import DdgSearchAdapter
from osint_agent.tools.usaspending import UsaSpendingAdapter
from osint_agent.tools.whois_lookup import WhoisAdapter
from osint_agent.tools.patents import PatentsAdapter
from osint_agent.tools.sbir import SbirAdapter
from osint_agent.tools.commoncrawl import CommonCrawlAdapter
from osint_agent.tools.peoplesearch import PeopleSearchAdapter
from osint_agent.tools.reddit import RedditAdapter
from osint_agent.tools.gravatar import GravatarAdapter
from osint_agent.tools.steam import SteamAdapter
from osint_agent.tools.ytdlp import YtDlpAdapter
from osint_agent.tools.crtsh import CrtshAdapter
from osint_agent.tools.dns_enum import DnsEnumAdapter
from osint_agent.tools.ip_whois import IpWhoisAdapter
from osint_agent.tools.crosslinked import CrossLinkedAdapter
from osint_agent.tools.builtwith_adapter import BuiltWithAdapter
from osint_agent.tools.littlesis import LittleSisAdapter
from osint_agent.tools.openpolicedata import OpenPoliceDataAdapter
from osint_agent.tools.propublica_nonprofit import ProPublicaNonprofitAdapter
from osint_agent.tools.wayback_ga import WaybackGaAdapter
from osint_agent.tools.documentcloud import DocumentCloudAdapter
from osint_agent.tools.fara import FaraAdapter
from osint_agent.tools.muckrock import MuckRockAdapter
from osint_agent.tools.congress import CongressAdapter


# Maps input types to which tools can handle them
INPUT_ROUTING = {
    "username": ["maigret", "reddit", "steam", "ddg_search"],
    "email": ["holehe", "gravatar", "theharvester", "maigret"],
    "phone": ["phoneinfoga"],
    "domain": ["theharvester", "whois", "crtsh", "dns_enum", "builtwith", "wayback_ga", "commoncrawl", "ddg_search"],
    "company": ["edgar", "usaspending", "sbir", "patents", "crosslinked", "littlesis", "propublica_nonprofit", "documentcloud", "fara", "muckrock", "ddg_search"],
    "person_name": ["openfec", "courtlistener", "edgar", "usaspending", "sbir", "patents", "peoplesearch", "littlesis", "documentcloud", "fara", "congress", "ddg_search"],
    "police_agency": ["openpolicedata"],
    "url": ["wayback", "commoncrawl", "yt-dlp"],
    "ip": ["ip_whois"],
    "image_file": ["exiftool"],
}


class ToolRegistry:
    """Central registry of all available OSINT tool adapters.

    Handles tool discovery, availability checking, and routing
    queries to appropriate tools based on input type.
    """

    def __init__(self, tool_config: dict[str, bool] | None = None):
        """Initialize with optional config to enable/disable specific tools.

        Args:
            tool_config: Dict of tool_name -> enabled. If None, all tools
                         that pass is_available() are enabled.
        """
        self._adapters: dict[str, ToolAdapter] = {}
        self._tool_config = tool_config or {}
        self._register_all()

    def _register_all(self):
        """Instantiate and register all known adapters."""
        all_adapters = [
            MaigretAdapter(),
            HoleheAdapter(),
            EdgarAdapter(),
            CourtListenerAdapter(),
            OpenFECAdapter(),
            WaybackAdapter(),
            ExifToolAdapter(),
            PhoneInfogaAdapter(),
            TheHarvesterAdapter(),
            DdgSearchAdapter(),
            UsaSpendingAdapter(),
            WhoisAdapter(),
            PatentsAdapter(),
            SbirAdapter(),
            CommonCrawlAdapter(),
            PeopleSearchAdapter(),
            RedditAdapter(),
            GravatarAdapter(),
            SteamAdapter(),
            YtDlpAdapter(),
            CrtshAdapter(),
            DnsEnumAdapter(),
            IpWhoisAdapter(),
            CrossLinkedAdapter(),
            BuiltWithAdapter(),
            LittleSisAdapter(),
            OpenPoliceDataAdapter(),
            ProPublicaNonprofitAdapter(),
            WaybackGaAdapter(),
            DocumentCloudAdapter(),
            FaraAdapter(),
            MuckRockAdapter(),
            CongressAdapter(),
        ]
        for adapter in all_adapters:
            name = adapter.name
            # Skip if explicitly disabled in config
            if name in self._tool_config and not self._tool_config[name]:
                continue
            self._adapters[name] = adapter

    def get(self, name: str) -> ToolAdapter | None:
        """Get a specific adapter by name."""
        return self._adapters.get(name)

    def available(self) -> dict[str, bool]:
        """Check which registered tools are actually available (installed + configured)."""
        return {name: adapter.is_available() for name, adapter in self._adapters.items()}

    def for_input_type(self, input_type: str) -> list[ToolAdapter]:
        """Get all available adapters that handle a given input type.

        Args:
            input_type: One of the keys in INPUT_ROUTING
                        (username, email, phone, domain, company, person_name, url, image_file).
        """
        tool_names = INPUT_ROUTING.get(input_type, [])
        return [
            self._adapters[name]
            for name in tool_names
            if name in self._adapters and self._adapters[name].is_available()
        ]

    def summary(self) -> str:
        """Human-readable summary of tool availability."""
        avail = self.available()
        lines = ["Tool Registry:"]
        for name, installed in sorted(avail.items()):
            status = "ready" if installed else "not available"
            lines.append(f"  {name}: {status}")
        ready = sum(1 for v in avail.values() if v)
        lines.append(f"\n{ready}/{len(avail)} tools available")
        return "\n".join(lines)
