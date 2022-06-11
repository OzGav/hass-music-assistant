"""Support Home Assistant media_player entities to be used as Players for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
from time import time
from typing import Optional, Tuple

from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.const import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MUSIC,
    SUPPORT_PLAY_MEDIA,
    SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON,
    SUPPORT_VOLUME_MUTE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    EVENT_STATE_CHANGED,
    STATE_IDLE,
    STATE_OFF,
    STATE_ON,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import Event
from homeassistant.util.dt import utcnow
from music_assistant import MusicAssistant
from music_assistant.models.enums import ContentType
from music_assistant.models.player import (
    DeviceInfo,
    Player,
    PlayerState,
    get_child_players,
    get_group_volume,
)

from .const import (
    ATTR_SOURCE_ENTITY_ID,
    CONF_PLAYER_ENTITIES,
    DEFAULT_NAME,
    DOMAIN,
    ESPHOME_DOMAIN,
    SLIMPROTO_DOMAIN,
    SLIMPROTO_EVENT,
    SONOS_DOMAIN,
)

LOGGER = logging.getLogger(__name__)

OFF_STATES = (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN, STATE_STANDBY)
UNAVAILABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)
CAST_DOMAIN = "cast"
CAST_MULTIZONE_MANAGER_KEY = "cast_multizone_manager"


GROUP_DOMAIN = "group"


STATE_MAPPING = {
    STATE_OFF: PlayerState.OFF,
    STATE_ON: PlayerState.IDLE,
    STATE_UNKNOWN: PlayerState.OFF,
    STATE_UNAVAILABLE: PlayerState.OFF,
    STATE_IDLE: PlayerState.IDLE,
    STATE_PLAYING: PlayerState.PLAYING,
    STATE_PAUSED: PlayerState.PAUSED,
    STATE_STANDBY: PlayerState.OFF,
}


class HassPlayer(Player):
    """Generic/base Mapping from Home Assistant Mediaplayer to Music Assistant Player."""

    use_mute_as_power: bool = False

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Initialize player."""
        self.hass = hass
        # use the (source) entity_id as player_id for now, to be improved later with unique_id ?
        self.player_id = entity_id
        self.entity_id = entity_id

        # grab a reference to the underlying entity
        entity_comp = hass.data.get(DATA_INSTANCES, {}).get(MP_DOMAIN)
        self.entity: MediaPlayerEntity = entity_comp.get_entity(entity_id)

        manufacturer = "Home Assistant"
        model = entity_id
        if entry := self.entity.registry_entry:
            # grab device entry
            if entry.device_id:
                dev_reg = dr.async_get(hass)
                device = dev_reg.async_get(entry.device_id)
                manufacturer = device.manufacturer
                model = device.model
        self._attr_device_info = DeviceInfo(manufacturer=manufacturer, model=model)
        self._attr_powered = False
        self.update_attributes()

    @property
    def name(self) -> str:
        """Return player name."""
        return self.entity.name if self.entity else self.entity_id

    @property
    def support_power(self) -> bool:
        """Return if this player supports power commands."""
        if entity := self.entity:
            return bool(entity.supported_features & SUPPORT_TURN_ON) and bool(
                entity.supported_features & SUPPORT_TURN_OFF
            )
        return False

    @property
    def powered(self) -> bool:
        """Return bool if this player is currently powered on."""
        if not self.available:
            return False
        if self.use_mute_as_power:
            return not self.volume_muted
        if self.support_power:
            return self.entity.state not in OFF_STATES
        return self._attr_powered

    @property
    def elapsed_time(self) -> float:
        """Return elapsed time of current playing media in seconds."""
        if not self.available:
            return 0
        # we need to return the corrected time here
        extra_attr = self.entity.extra_state_attributes or {}
        media_position = extra_attr.get(
            "media_position_mass", self.entity.media_position
        )
        last_upd = self.entity.media_position_updated_at
        # LOGGER.debug("[%s] - media_position: %s - updated_at: %s", self.name, self.entity.media_position,  self.entity.media_position_updated_at)
        if last_upd is None or media_position is None:
            return 0
        diff = (utcnow() - last_upd).seconds
        return media_position + diff

    @property
    def current_url(self) -> str:
        """Return URL that is currently loaded in the player."""
        if not self.entity:
            return ""
        return self.entity.media_content_id

    @property
    def state(self) -> PlayerState:
        """Return current state of player."""
        if not self.available:
            return PlayerState.OFF
        if not self.powered:
            return PlayerState.OFF
        if self.entity.state == PlayerState.OFF and self.powered:
            return PlayerState.IDLE
        return STATE_MAPPING.get(self.entity.state, PlayerState.OFF)

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        if not self.available:
            return 0
        if self.is_group:
            return get_group_volume(self)
        if self.entity and self.entity.support_volume_set:
            return self.entity.volume_level * 100
        return 100

    @property
    def volume_muted(self) -> bool:
        """Return current mute mode of player."""
        if not self.available:
            return False
        if self.entity and self.entity.support_volume_mute:
            return self.entity.is_volume_muted
        return self._attr_volume_muted

    @property
    def supported_content_types(self) -> Tuple[ContentType]:
        """Return the content types this player supports."""
        if not self.is_group:
            return self._attr_supported_content_types
        # return contenttypes that are supported by all child players
        return tuple(
            content_type
            for content_type in ContentType
            if all(
                (
                    content_type in child_player.supported_content_types
                    for child_player in get_child_players(self, False, False)
                )
            )
        )

    @property
    def supported_sample_rates(self) -> Tuple[int]:
        """Return the sample rates this player supports."""
        if not self.is_group:
            return self._attr_supported_sample_rates
        return tuple(
            sample_rate
            for sample_rate in (44100, 48000, 88200, 96000)
            if all(
                (
                    sample_rate in child_player.supported_sample_rates
                    for child_player in get_child_players(self, False, False)
                )
            )
        )

    @callback
    def on_hass_event(self, event: Event) -> None:
        """Call on Home Assistant event."""
        self.update_attributes()
        if event.event_type == "state_changed":
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            if old_state and new_state:
                self.on_state_changed(old_state, new_state)
        self.update_state()

    @callback
    def on_state_changed(self, old_state: State, new_state: State) -> None:
        """Call when state changes from HA player."""
        LOGGER.debug(
            "[%s] state_changed - old: %s - new: %s",
            self.entity_id,
            old_state.state,
            new_state.state,
        )

    @callback
    def update_attributes(self) -> None:
        """Update attributes of this player."""
        if not self.entity:
            entity_comp = self.hass.data.get(DATA_INSTANCES, {}).get(MP_DOMAIN)
            self.entity: MediaPlayerEntity = entity_comp.get_entity(self.entity_id)
        if not self.entity:
            self._attr_available = False
        else:
            self._attr_available = self.entity.available

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        LOGGER.debug("[%s] play_url: %s", self.entity_id, url)
        if self.volume_muted:
            await self.volume_mute(False)
        await self.entity.async_play_media(
            MEDIA_TYPE_MUSIC,
            url,
        )

    async def stop(self) -> None:
        """Send STOP command to player."""
        LOGGER.debug("[%s] stop", self.entity_id)
        await self.entity.async_media_stop()

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        LOGGER.debug("[%s] play", self.entity_id)
        await self.entity.async_media_play()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        LOGGER.debug("[%s] pause", self.entity_id)
        await self.entity.async_media_pause()

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        LOGGER.debug("[%s] power: %s", self.entity_id, powered)
        # send stop if this player is active queue
        if not powered and self.active_queue.queue_id == self.player_id:
            if self.state == PlayerState.PLAYING:
                await self.active_queue.stop()
        if self.use_mute_as_power:
            await self.volume_mute(not powered)
        elif (
            powered
            and self.entity
            and bool(self.entity.supported_features & SUPPORT_TURN_ON)
        ):
            # regular turn_on command
            await self.entity.async_turn_on()
        elif (
            not powered
            and self.entity
            and bool(self.entity.supported_features & SUPPORT_TURN_OFF)
        ):
            # regular turn_off command
            await self.entity.async_turn_off()
        else:
            # no power support on device
            self._attr_powered = powered
            self.update_state()
        # check group power: power off group when last player powers down
        if not powered:
            self.check_group_power()

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        LOGGER.debug("[%s] volume_set: %s", self.entity_id, volume_level)
        if self.entity and self.entity.support_volume_set:
            await self.entity.async_set_volume_level(volume_level / 100)

    async def volume_mute(self, muted: bool) -> None:
        """Send volume mute command to player."""
        # for players that do not support mute, we fake mute with volume
        if not bool(self.entity.supported_features & SUPPORT_VOLUME_MUTE):
            await super().volume_mute(muted)
            return
        await self.entity.async_mute_volume(muted)

    async def next_track(self) -> None:
        """Send next_track command to player."""
        LOGGER.debug("[%s] next_track", self.entity_id)
        await self.entity.async_media_next_track()

    async def previous_track(self) -> None:
        """Send previous_track command to player."""
        LOGGER.debug("[%s] previous_track", self.entity_id)
        await self.entity.async_media_previous_track()

    def check_group_power(self) -> None:
        """Check if groupplayer can be turned off when all childs are powered off."""
        # convenience helper:
        # power off group player if last child player turns off
        for group_id in self.group_parents:
            group_player = self.mass.players.get_player(group_id)
            if not group_player:
                continue
            if not group_player.powered:
                continue
            powered_childs = set()
            for child_player in get_child_players(group_player):
                if child_player.player_id == self.player_id:
                    continue
                if child_player.powered:
                    powered_childs.add(child_player.player_id)
            if len(powered_childs) == 0:
                self.mass.create_task(group_player.power(False))


class SlimprotoPlayer(HassPlayer):
    """Representation of Hass player from Squeezebox Local integration."""

    # TODO: read max sample rate and supported codecs from player

    def __init__(self, *args, **kwargs) -> None:
        """Initialize player."""
        super().__init__(*args, **kwargs)
        self.slimserver = self.hass.data[SLIMPROTO_DOMAIN]
        self._unsubs = [
            self.hass.bus.async_listen(SLIMPROTO_EVENT, self.on_squeezebox_event)
        ]

    @callback
    def on_remove(self) -> None:
        """Call when player is about to be removed (cleaned up) from player manager."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def on_squeezebox_event(self, event: Event) -> None:
        """Handle special events from squeezebox players."""
        if event.data["entity_id"] != self.entity_id:
            return
        cmd = event.data["command_str"]
        if cmd == "playlist index +1":
            self.hass.create_task(self.active_queue.next())
        if cmd == "playlist index -1":
            self.hass.create_task(self.active_queue.previous())


class ESPHomePlayer(HassPlayer):
    """Representation of Hass player from ESPHome integration."""

    _attr_supported_content_types: Tuple[ContentType] = (ContentType.MP3,)
    _attr_supported_sample_rates: Tuple[int] = (44100, 48000)


class CastPlayer(HassPlayer):
    """Representation of Hass player from cast integration."""

    _attr_supported_sample_rates: Tuple[int] = (44100, 48000, 88200, 96000)

    def __init__(self, *args, **kwargs) -> None:
        """Initialize cast player control."""
        super().__init__(*args, **kwargs)
        self.cast_uuid = self.entity.registry_entry.unique_id
        if self._attr_device_info.model == "Google Cast Group":
            # this is a cast group
            self._attr_is_group = True

    @callback
    def update_attributes(self) -> None:
        """Update attributes of this player."""
        super().update_attributes()
        self.use_mute_as_power = not (self.is_group or len(self.group_parents) == 0)
        if not self._attr_is_group:
            return
        # this is a bit hacky to get the group members
        # TODO: create PR to add these as state attributes to the cast integration
        # pylint: disable=protected-access
        if CAST_MULTIZONE_MANAGER_KEY not in self.hass.data:
            return
        mz_mgr = self.hass.data[CAST_MULTIZONE_MANAGER_KEY]
        if self.cast_uuid not in mz_mgr._groups:
            return
        mz_ctrl = mz_mgr._groups[self.cast_uuid]["listener"]._mz
        child_players = []
        ent_reg = er.async_get(self.hass)
        for cast_uuid in mz_ctrl.members:
            if entity_id := ent_reg.entities.get_entity_id(
                (MP_DOMAIN, CAST_DOMAIN, cast_uuid)
            ):
                child_players.append(entity_id)
        self._attr_group_childs = child_players

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        # pylint: disable=import-outside-toplevel,protected-access
        from homeassistant.components.cast.media_player import quick_play

        cast = self.entity._chromecast
        app_data = {
            "media_id": url,
            "media_type": f"audio/{self.active_queue.settings.stream_type.value}",
            "enqueue": False,
            "title": f" Streaming from {DEFAULT_NAME}",
        }
        await self.hass.async_add_executor_job(
            quick_play, cast, "default_media_receiver", app_data
        )
        # enqueue second item to allow on-player control of next
        # (or shout next track from google assistant)
        app_data["enqueue"] = True
        await self.hass.async_add_executor_job(
            quick_play, cast, "default_media_receiver", app_data
        )


class SonosPlayer(HassPlayer):
    """Representation of Hass player from Sonos integration."""

    _attr_supported_sample_rates: Tuple[int] = (44100, 48000)
    _sonos_paused: bool = False

    def __init__(self, *args, **kwargs) -> None:
        """Initialize."""
        self._last_info_fetch = 0
        self._paused = False
        super().__init__(*args, **kwargs)

    @property
    def state(self) -> PlayerState:
        """Return current PlayerState of player."""
        # pylint: disable=protected-access
        # a sonos player is always either playing or paused
        # consider idle if nothing is playing and we did not pause
        if self.entity.state == STATE_PAUSED and not self._sonos_paused:
            return PlayerState.IDLE
        return super().state

    @callback
    def on_state_changed(self, old_state: State, new_state: State) -> None:
        """Call when state changes from HA player."""
        super().on_state_changed(old_state, new_state)
        self.hass.create_task(self.poll_sonos(True))

    @property
    def elapsed_time(self) -> float:
        """Return elapsed time of current playing media in seconds."""
        # elapsed_time is read by queue controller every second while playing
        # add the poll task here to make sure we have accurate info
        self.hass.create_task(self.poll_sonos())
        return super().elapsed_time

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        self._sonos_paused = False
        await super().play()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        self._sonos_paused = True
        await super().pause()

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        self._sonos_paused = False

        def _play_url():
            soco = self.entity.coordinator.soco
            meta = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
                '<item id="1" parentID="0" restricted="1">'
                "<dc:title>Streaming from Music Assistant</dc:title>"
                "<dc:creator></dc:creator>"
                "<upnp:album></upnp:album>"
                "<upnp:channelName>Music Assistant</upnp:channelName>"
                "<upnp:channelNr>0</upnp:channelNr>"
                "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
                f'<res protocolInfo="http-get:*:audio/flac:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{url}</res>'
                "</item>"
                "</DIDL-Lite>"
            )
            soco.play_uri(url, meta=meta, force_radio=False)

        await self.hass.loop.run_in_executor(None, _play_url)
        await self.poll_sonos(True)

    async def poll_sonos(self, force: Optional[bool] = None) -> None:
        """Call when the PlayerQueue polls the player for accurate info."""
        if not self.entity:
            return

        def poll_sonos():
            if self.entity.speaker.is_coordinator:
                self.entity.media.poll_media()
                LOGGER.debug("poll sonos")

        if force is None:
            force = (
                self.entity.media_position is None
                and self.entity.state == STATE_PLAYING
            )
        if force or (time() - self._last_info_fetch) > 30:
            await self.hass.loop.run_in_executor(None, poll_sonos)
            self._last_info_fetch = time()


class HassGroupPlayer(HassPlayer):
    """Mapping from Home Assistant Grouped Mediaplayer to Music Assistant Player."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize player."""
        super().__init__(*args, **kwargs)
        self._attr_is_group = True
        self._attr_use_multi_stream = True
        self._attr_current_url = ""
        self._attr_device_info = DeviceInfo(
            manufacturer="Home Assistant", model="Media Player Group"
        )
        self.update_attributes()

    @property
    def current_url(self) -> PlayerState:
        """Return the current_url of the grouped player."""
        # grab details from first (powered) group child
        for child_player in get_child_players(self, True):
            if not child_player.current_url:
                continue
            return child_player.current_url
        return super().state

    @property
    def powered(self) -> bool:
        """Return the current_url of the grouped player."""
        # powered if all powered players have same url loaded
        prev_url = None
        powered_players = 0
        for child_player in get_child_players(self, True):
            if not child_player.current_url:
                continue
            powered_players += 1
            if not prev_url:
                prev_url = child_player.current_url
            if prev_url != child_player.current_url:
                return False
        return powered_players > 0

    @property
    def elapsed_time(self) -> float:
        """Return the corrected/precise elsapsed time of the grouped player."""
        # grab details from first (powered) group child
        for child_player in get_child_players(self, True):
            if not child_player.current_url:
                continue
            return child_player.elapsed_time
        return 0

    async def stop(self) -> None:
        """Send STOP command to player."""
        # redirect command to all child players
        await asyncio.gather(*[x.stop() for x in get_child_players(self, True)])

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        # redirect command to all child players
        await asyncio.gather(*[x.play() for x in get_child_players(self, True)])

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        # redirect command to all child players
        await asyncio.gather(*[x.pause() for x in get_child_players(self, True)])

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        # redirect command to all child players
        await asyncio.gather(
            *[x.power(powered) for x in get_child_players(self, False)]
        )

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        self._attr_current_url = url
        # redirect command to all child players
        await asyncio.gather(*[x.play_url(url) for x in get_child_players(self, True)])

    @callback
    def on_hass_event(self, event: Event) -> None:
        """Call on Home Assistant event."""
        self.update_attributes()
        self.update_state()

    @callback
    def update_attributes(self) -> None:
        """Call when player state is about to be updated in the player manager."""
        hass_state = self.hass.states.get(self.entity_id)
        self._attr_available = hass_state.state not in UNAVAILABLE_STATES
        self._attr_name = hass_state.name
        self._attr_powered = hass_state.state not in OFF_STATES

        # collect the group childs, be prepared for the usecase where the user actually
        # added a mass player to a group, translate that to the underlying entity.
        group_childs = []
        for entity_id in hass_state.attributes.get(ATTR_ENTITY_ID, []):
            if source_id := self._get_source_entity_id(entity_id):
                group_childs.append(source_id)
        self._attr_group_childs = group_childs

    def _get_source_entity_id(self, entity_id: str) -> str | None:
        """Return source entity_id from child entity_id."""
        if hass_state := self.hass.states.get(entity_id):
            # if entity is actually already mass entity, return the source entity
            if source_id := hass_state.attributes.get(ATTR_SOURCE_ENTITY_ID):
                return source_id
            return entity_id
        return None


PLAYER_MAPPING = {
    CAST_DOMAIN: CastPlayer,
    SLIMPROTO_DOMAIN: SlimprotoPlayer,
    ESPHOME_DOMAIN: ESPHomePlayer,
    SONOS_DOMAIN: SonosPlayer,
    GROUP_DOMAIN: HassGroupPlayer,
}


async def async_register_player_control(
    hass: HomeAssistant, mass: MusicAssistant, entity_id: str
) -> HassPlayer | None:
    """Register hass media_player entity as player control on Music Assistant."""

    # check for existing player first if already registered
    if player := mass.players.get_player(entity_id, True):
        return player

    entity = hass.states.get(entity_id)
    if entity is None or entity.attributes is None:
        return

    if not (entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0) & SUPPORT_PLAY_MEDIA):
        return

    ent_reg = er.async_get(hass)
    player = None
    # Integration specific player controls
    entry_platform = None
    if ent_entry := ent_reg.async_get(entity_id):
        entry_platform = ent_entry.platform
    if entry_platform == DOMAIN:
        # this is already a Music assistant player
        return

    # load player specific mapping or generic one
    player_cls = PLAYER_MAPPING.get(entry_platform, HassPlayer)
    player = player_cls(hass, entity_id)
    await mass.players.register_player(player)
    return player


async def async_register_player_controls(
    hass: HomeAssistant, mass: MusicAssistant, entry: ConfigEntry
):
    """Register hass entities as player controls on Music Assistant."""
    # allowed_entities not configured = not filter (=all)
    allowed_entities = entry.options.get(CONF_PLAYER_ENTITIES)

    async def async_hass_state_event(event: Event) -> None:
        """Handle hass state-changed events to update registered PlayerControls."""
        entity_id: str = event.data[ATTR_ENTITY_ID]

        if not entity_id.startswith(MP_DOMAIN):
            return

        # handle existing source player
        if source_player := mass.players.get_player(entity_id, True):
            source_player.on_hass_event(event)
            return
        # entity not (yet) registered
        if allowed_entities is None or entity_id in allowed_entities:
            await async_register_player_control(hass, mass, entity_id)

    # register event listener
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_STATE_CHANGED, async_hass_state_event)
    )
    # register all current entities
    for entity in hass.states.async_all(MEDIA_PLAYER_DOMAIN):
        if allowed_entities is None or entity.entity_id in allowed_entities:
            if entity.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            await async_register_player_control(hass, mass, entity.entity_id)
