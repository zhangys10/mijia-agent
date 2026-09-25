from collections.abc import Iterable

from mijia_agent.models import HomeCapabilities
from mijia_assistant.models import AssistantContext

from .base import Capability
from .home import DeviceStatusCapability, HomeEnvironmentCapability, HomeExposureDiscoveryCapability


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[Capability] = ()):
        items = list(capabilities)
        if len({item.name for item in items}) != len(items):
            raise ValueError("duplicate capability name")
        self._capabilities = tuple(items)

    async def for_context(
        self, ctx: AssistantContext, manifest: HomeCapabilities | None = None
    ) -> dict[str, Capability]:
        available: dict[str, Capability] = {}
        home_templates: list[HomeEnvironmentCapability | DeviceStatusCapability] = []
        for capability in self._capabilities:
            if await capability.is_available(ctx):
                if isinstance(capability, (HomeEnvironmentCapability, DeviceStatusCapability)):
                    home_templates.append(capability)
                else:
                    available[capability.name] = capability
        if manifest is None and home_templates:
            discovery = HomeExposureDiscoveryCapability(home_templates[0].tools)
            available[discovery.name] = discovery
        elif manifest is not None:
            for template in home_templates:
                bound = template.bind(manifest)
                if bound is not None:
                    available[bound.name] = bound
        return available
