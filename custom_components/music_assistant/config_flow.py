"""Config flow for Music Assistant integration."""

from typing import Any, Dict, List


import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback


from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector


from .const import (
    CONF_CREATE_MASS_PLAYERS,
    CONF_HIDE_SOURCE_PLAYERS,
    CONF_PLAYER_ENTITIES,
    CONF_QOBUZ_ENABLED,
    CONF_QOBUZ_PASSWORD,
    CONF_QOBUZ_USERNAME,
    CONF_SPOTIFY_ENABLED,
    CONF_SPOTIFY_PASSWORD,
    CONF_SPOTIFY_USERNAME,
    CONF_TUNEIN_ENABLED,
    CONF_TUNEIN_USERNAME,
    DEFAULT_NAME,
    DOMAIN,
)


@callback
def async_get_mass_entities(hass: HomeAssistant):
    """Return all media_player entities created by the Music Assistant integration."""
    ent_reg = er.async_get(hass)
    return [
        x.entity_id
        for x in ent_reg.entities.values()
        if x.domain == MP_DOMAIN and x.platform == DOMAIN
    ]


@callback
def hide_player_entities(hass: HomeAssistant, entity_ids: List[str], hide: bool):
    """Hide/unhide media_player entities that are used as source for Music Assistant."""
    # Hide the wrapped entry if registered
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        entity_entry = registry.async_get(entity_id)
        if entity_entry is None:
            continue
        if entity_entry.hidden and not hide:
            registry.async_update_entity(entity_id, hidden_by=None)
        elif not entity_entry.hidden and hide:
            registry.async_update_entity(
                entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
            )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Music Assistant."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    data: Dict[str, Any] = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle getting base config from the user."""

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return await self.async_step_music(user_input)

        control_entities = [x.entity_id for x in self.hass.states.async_all(MP_DOMAIN)]
        exclude_entities = async_get_mass_entities(self.hass)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PLAYER_ENTITIES,
                        default=control_entities,
                    ): selector.selector(
                        {
                            "entity": {
                                "domain": "media_player",
                                "multiple": True,
                                "exclude_entities": exclude_entities,
                            }
                        }
                    ),
                    vol.Required(
                        CONF_HIDE_SOURCE_PLAYERS, default=False
                    ): selector.selector({"boolean": {}}),
                    vol.Required(
                        CONF_CREATE_MASS_PLAYERS, default=True
                    ): selector.selector({"boolean": {}}),
                }
            ),
            last_step=False,
        )

    async def async_step_music(self, user_input=None):
        """Handle getting music provider config from the user."""

        if self.data is None and user_input:
            self.data = user_input
        elif user_input:
            # config complete, store entry
            self.data.update(user_input)
            hide_player_entities(
                self.hass,
                self.data[CONF_PLAYER_ENTITIES],
                self.data[CONF_HIDE_SOURCE_PLAYERS],
            )
            return self.async_create_entry(title=DEFAULT_NAME, data={}, options=self.data)

        return self.async_show_form(
            step_id="music",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SPOTIFY_ENABLED, default=False): bool,
                    vol.Optional(CONF_SPOTIFY_USERNAME): str,
                    vol.Optional(CONF_SPOTIFY_PASSWORD): str,
                    vol.Required(CONF_QOBUZ_ENABLED, default=False): bool,
                    vol.Optional(CONF_QOBUZ_USERNAME): str,
                    vol.Optional(CONF_QOBUZ_PASSWORD): str,
                    vol.Required(CONF_TUNEIN_ENABLED, default=False): bool,
                    vol.Optional(CONF_TUNEIN_USERNAME): str,
                }
            ),
            last_step=True,
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """OptionsFlow handler."""

    data: Dict[str, Any] = None

    def __init__(self, config_entry: config_entries.ConfigEntry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Handle getting base config from the user."""

        if user_input is not None:
            return await self.async_step_music(user_input)

        conf = self.config_entry.options
        control_entities = [x.entity_id for x in self.hass.states.async_all(MP_DOMAIN)]
        exclude_entities = async_get_mass_entities(self.hass)

        # filter any non existing device id's from the list
        cur_ids = [
            item
            for item in conf.get(CONF_PLAYER_ENTITIES, [])
            if item in control_entities
        ]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PLAYER_ENTITIES,
                        default=cur_ids,
                    ): selector.selector(
                        {
                            "entity": {
                                "domain": "media_player",
                                "multiple": True,
                                "exclude_entities": exclude_entities,
                            }
                        }
                    ),
                    vol.Required(
                        CONF_HIDE_SOURCE_PLAYERS,
                        default=conf.get(CONF_HIDE_SOURCE_PLAYERS, False),
                    ): selector.selector({"boolean": {}}),
                    vol.Required(
                        CONF_CREATE_MASS_PLAYERS,
                        default=conf.get(CONF_CREATE_MASS_PLAYERS, True),
                    ): selector.selector({"boolean": {}}),
                }
            ),
            last_step=False,
        )

    async def async_step_music(self, user_input=None):
        """Handle getting music provider config from the user."""

        if self.data is None and user_input:
            self.data = user_input
        elif user_input:
            # config complete, store entry
            self.data.update(user_input)
            hide_player_entities(
                self.hass,
                self.data[CONF_PLAYER_ENTITIES],
                self.data[CONF_HIDE_SOURCE_PLAYERS],
            )
            return self.async_create_entry(title="", data={**self.data})

        conf = self.config_entry.options
        return self.async_show_form(
            step_id="music",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SPOTIFY_ENABLED,
                        default=conf.get(CONF_SPOTIFY_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        CONF_SPOTIFY_USERNAME, default=conf.get(CONF_SPOTIFY_USERNAME)
                    ): str,
                    vol.Optional(
                        CONF_SPOTIFY_PASSWORD, default=conf.get(CONF_SPOTIFY_PASSWORD)
                    ): str,
                    vol.Required(
                        CONF_QOBUZ_ENABLED, default=conf.get(CONF_QOBUZ_ENABLED, False)
                    ): bool,
                    vol.Optional(
                        CONF_QOBUZ_USERNAME, default=conf.get(CONF_QOBUZ_USERNAME)
                    ): str,
                    vol.Optional(
                        CONF_QOBUZ_PASSWORD, default=conf.get(CONF_QOBUZ_PASSWORD)
                    ): str,
                    vol.Required(
                        CONF_TUNEIN_ENABLED,
                        default=conf.get(CONF_TUNEIN_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        CONF_TUNEIN_USERNAME, default=conf.get(CONF_TUNEIN_USERNAME)
                    ): str,
                }
            ),
            last_step=True,
        )