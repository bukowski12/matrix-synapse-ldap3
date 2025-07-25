# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import ssl
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import ldap3
import ldap3.core.exceptions
import synapse
from pkg_resources import parse_version
from synapse.module_api import ModuleApi
from synapse.types import JsonDict
from twisted.internet import threads

__version__ = "0.3.0"

logger = logging.getLogger(__name__)


class ActiveDirectoryUPNException(Exception):
    """Raised in case the user's login credentials cannot be mapped to a UPN"""

    pass


class LDAPMode:
    SIMPLE: Tuple[str] = ("simple",)
    SEARCH: Tuple[str] = ("search",)

    LIST: Tuple[Tuple[str], ...] = (SIMPLE, SEARCH)


@dataclass
class _LdapConfig:
    enabled: bool
    mode: Tuple[str]
    uri: Union[str, List[str]]
    start_tls: bool
    validate_cert: bool
    tls_options: Dict[str, Any]
    base: str
    attributes: Dict[str, str]
    bind_dn: Optional[str] = None
    bind_password: Optional[str] = None
    filter: Optional[str] = None
    active_directory: Optional[str] = None
    default_domain: Optional[str] = None
    user_mapping: Optional[Dict[str, str]] = None


SUPPORTED_LOGIN_TYPE: str = "m.login.password"
SUPPORTED_LOGIN_FIELDS: Tuple[str, ...] = ("password",)


class LdapAuthProvider:
    def __init__(self, config: _LdapConfig, account_handler: ModuleApi):
        self.account_handler: ModuleApi = account_handler

        self.ldap_mode = config.mode
        self.ldap_uris = [config.uri] if isinstance(config.uri, str) else config.uri
        if config.tls_options:
            self.ldap_tls = ldap3.Tls(**config.tls_options)
        else:
            self.ldap_tls = ldap3.Tls(
                validate=ssl.CERT_REQUIRED if config.validate_cert else ssl.CERT_NONE
            )
        self.ldap_start_tls = config.start_tls
        self.ldap_base = config.base
        self.ldap_attributes = config.attributes
        if self.ldap_mode == LDAPMode.SEARCH:
            self.ldap_bind_dn = config.bind_dn
            self.ldap_bind_password = config.bind_password
            self.ldap_filter = config.filter

        self.ldap_active_directory = config.active_directory
        if self.ldap_active_directory:
            self.ldap_default_domain = config.default_domain
            # Either: the Active Directory root domain (type str); empty string in case
            # of error; or None if there was no attempt to fetch root domain yet
            self.ldap_root_domain = None  # type: Optional[str]

        # User mapping configuration
        self.user_mapping = config.user_mapping

    def get_supported_login_types(self) -> Dict[str, Tuple[str, ...]]:
        return {SUPPORTED_LOGIN_TYPE: SUPPORTED_LOGIN_FIELDS}

    def _apply_user_mapping(self, localpart: str) -> str:
        """Apply user mapping configuration to transform localpart.

        Args:
            localpart: Original localpart from LDAP authentication

        Returns:
            Transformed localpart according to user_mapping configuration
        """
        if not self.user_mapping:
            return localpart

        localpart_template = self.user_mapping.get("localpart_template")
        if not localpart_template:
            return localpart

        try:
            # Apply template transformation
            mapped_localpart = localpart_template.format(localpart=localpart)
            logger.debug("Mapped localpart '%s' to '%s' using template '%s'",
                        localpart, mapped_localpart, localpart_template)
            return mapped_localpart
        except (KeyError, ValueError) as e:
            logger.warning("Failed to apply user mapping template '%s' to localpart '%s': %s",
                          localpart_template, localpart, e)
            return localpart

    async def _reverse_user_mapping(self, mapped_localpart: str) -> str:
        """Reverse user mapping to get original localpart for LDAP queries.

        Uses user_external_ids table to find the original LDAP localpart.

        Args:
            mapped_localpart: Mapped localpart (e.g., 'u790159')

        Returns:
            Original localpart for LDAP queries (e.g., '790159')
        """
        if not self.user_mapping:
            return mapped_localpart

        # Get original localpart from database
        try:
            original_from_db = await self._get_original_localpart(mapped_localpart)
            if original_from_db:
                logger.debug("Found original localpart '%s' in database for '%s'",
                            original_from_db, mapped_localpart)
                return original_from_db
        except Exception as e:
            logger.warning("Failed to get original localpart from database for '%s': %s",
                          mapped_localpart, e)

        # If not found in database, assume it's already the original
        logger.debug("No original localpart found for '%s', assuming it's already original",
                    mapped_localpart)
        return mapped_localpart

    async def check_auth(
        self, username: str, login_type: str, login_dict: Dict[str, Any]
    ) -> Optional[str]:
        """Attempt to authenticate a user against an LDAP Server
        and register an account if none exists.

        Returns:
            Canonical user ID if authentication against LDAP was successful,
            or None if authentication was not successful.
        """
        password: str = login_dict["password"]
        # According to section 5.1.2. of RFC 4513 an attempt to log in with
        # non-empty DN and empty password is called Unauthenticated
        # Authentication Mechanism of Simple Bind which is used to establish
        # an anonymous authorization state and not suitable for user
        # authentication.
        if not password:
            return None

        if username.startswith("@") and ":" in username:
            # username is of the form @foo:bar.com
            username = username.split(":", 1)[0][1:]

        # If username is already mapped (from previous login), reverse it for LDAP queries
        original_username = await self._reverse_user_mapping(username)

        # Used in LDAP queries as value of ldap_attributes['uid'] attribute.
        uid_value = original_username
        # Default display name for the user, if a new account is registered.
        default_display_name = original_username
        # Local part of Matrix ID which will be used in registration process
        localpart = original_username

        if self.ldap_active_directory:
            try:
                (login, domain, localpart) = await self._map_login_to_upn(username)
                uid_value = login + "@" + domain
                default_display_name = login
            except ActiveDirectoryUPNException:
                return None

        try:
            server = self._get_server()
            logger.debug("Attempting LDAP connection with %s", self.ldap_uris)

            if self.ldap_mode == LDAPMode.SIMPLE:
                bind_dn = "{prop}={value},{base}".format(
                    prop=self.ldap_attributes["uid"],
                    value=uid_value,
                    base=self.ldap_base,
                )
                result, conn = await self._ldap_simple_bind(
                    server=server, bind_dn=bind_dn, password=password
                )
                logger.debug(
                    "LDAP authentication method simple bind returned: %s (conn: %s)",
                    result,
                    conn,
                )
                if not result:
                    return None
            elif self.ldap_mode == LDAPMode.SEARCH:
                filters = [(self.ldap_attributes["uid"], uid_value)]
                result, conn, _ = await self._ldap_authenticated_search(
                    server=server, password=password, filters=filters
                )
                logger.debug(
                    "LDAP auth method authenticated search returned: %s (conn: %s)",
                    result,
                    conn,
                )
                if not result:
                    return None
            else:  # pragma: no cover
                raise RuntimeError(
                    "Invalid LDAP mode specified: {mode}".format(mode=self.ldap_mode)
                )

            # conn is present because result is True in both cases before
            # control flows to this point
            assert conn is not None

            try:
                logger.info("User authenticated against LDAP server: %s", conn)
            except NameError:  # pragma: no cover
                logger.warning(
                    "Authentication method yielded no LDAP connection, aborting!"
                )
                return None

            # Apply user mapping to localpart before checking existence
            mapped_localpart = self._apply_user_mapping(localpart)

            # First try to find existing user by original LDAP localpart
            existing_user_id = await self._find_user_by_original_localpart(localpart)

            if existing_user_id:
                # User exists with this original LDAP ID, return existing user
                logger.debug("Found existing user '%s' for original localpart '%s'",
                            existing_user_id, localpart)
                return existing_user_id

            # Get full user id from mapped localpart
            user_id = self.account_handler.get_qualified_user_id(mapped_localpart)

            # check if user with mapped user_id exists (fallback for users without stored original ID)
            canonical_user_id = await self.account_handler.check_user_exists(user_id)
            if canonical_user_id:
                # exists, authentication complete
                if hasattr(conn, "unbind"):
                    await threads.deferToThread(conn.unbind)
                return canonical_user_id

            else:
                # does not exist, register
                if self.ldap_mode == LDAPMode.SEARCH:
                    # search enabled, fetch metadata for account creation from
                    # existing ldap connection
                    filters = [(self.ldap_attributes["uid"], uid_value)]

                    result, conn, response = await self._ldap_authenticated_search(
                        server=server,
                        password=password,
                        filters=filters,
                    )

                    # These results will always return an array
                    display_name = response["attributes"].get(
                        self.ldap_attributes["name"], [localpart]
                    )
                    display_name = (
                        display_name[0]
                        if len(display_name) == 1
                        else default_display_name
                    )

                    mail = response["attributes"].get(
                        self.ldap_attributes["mail"], [None]
                    )
                    mail = mail[0] if len(mail) == 1 else None
                else:
                    # search disabled, register account with basic information
                    display_name = default_display_name
                    mail = None

                # Register the user with mapped localpart
                user_id = await self.register_user(
                    mapped_localpart.lower(), display_name, mail,
                    already_mapped=True, original_localpart=localpart.lower()
                )

                return user_id

            return None

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning("Error during ldap authentication: %s", e)
            return None

    async def check_3pid_auth(
        self, medium: str, address: str, password: str
    ) -> Optional[str]:
        """Handle authentication against thirdparty login types, such as email

        Args:
            medium: Medium of the 3PID (e.g email, msisdn).
            address: Address of the 3PID (e.g bob@example.com for email).
            password: The provided password of the user.

        Returns:
            user_id: ID of the user if authentication successful. None otherwise.
        """
        if self.ldap_mode != LDAPMode.SEARCH:
            logger.debug(
                "3PID LDAP login/register attempted but LDAP search mode "
                "not enabled. Bailing."
            )
            return None

        # We currently only support email
        if medium != "email":
            return None

        # Talk to LDAP and check if this email/password combo is correct
        try:
            server = self._get_server()
            logger.debug("Attempting LDAP connection with %s", self.ldap_uris)

            search_filter = [(self.ldap_attributes["mail"], address)]
            result, conn, response = await self._ldap_authenticated_search(
                server=server,
                password=password,
                filters=search_filter,
            )

            logger.debug(
                "LDAP auth method authenticated search returned: "
                "%s (conn: %s) (response: %s)",
                result,
                conn,
                response,
            )

            # Close connection
            if hasattr(conn, "unbind"):
                await threads.deferToThread(conn.unbind)  # type: ignore[union-attr]

            if not result:
                return None

            # Extract the username from the search response from the LDAP server
            localpart = response["attributes"].get(self.ldap_attributes["uid"], [None])
            localpart = localpart[0] if len(localpart) == 1 else None
            if self.ldap_active_directory and localpart and "@" in localpart:
                (login, domain) = localpart.lower().rsplit("@", 1)
                localpart = login + "/" + domain

                if (
                    self.ldap_default_domain
                    and domain.lower() == self.ldap_default_domain.lower()
                ):
                    # Users in default AD domain don't have `/domain` suffix
                    localpart = login

            givenName = response["attributes"].get(
                self.ldap_attributes["name"], [localpart]
            )
            givenName = givenName[0] if len(givenName) == 1 else localpart

            # Register the user
            user_id = await self.register_user(localpart, givenName, address)

            return user_id

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning("Error during ldap authentication: %s", e)
            raise

    async def register_user(self, localpart: str, name: str, email_address: str, already_mapped: bool = False, original_localpart: str = None) -> str:
        """Register a Synapse user, first checking if they exist.

        Args:
            localpart: Localpart of the user to register on this homeserver.
            name: Full name of the user.
            email_address: Email address of the user.
            already_mapped: If True, localpart is already mapped and won't be mapped again.
            original_localpart: Original LDAP localpart (for storing in user_external_ids).

        Returns:
            user_id: User ID of the newly registered user.
        """
        # Apply user mapping to localpart before registration (unless already mapped)
        if already_mapped:
            mapped_localpart = localpart
            # If original_localpart not provided, we can't store it
            if original_localpart is None:
                original_localpart = localpart  # This might not be correct, but it's our best guess
        else:
            original_localpart = localpart
            mapped_localpart = self._apply_user_mapping(localpart)

        # Get full user id from mapped localpart
        user_id = self.account_handler.get_qualified_user_id(mapped_localpart)

        if await self.account_handler.check_user_exists(user_id):
            # exists, authentication complete
            return user_id

        # register an email address if one exists
        emails = [email_address] if email_address is not None else []

        # create account
        # check if we're running a version of synapse that supports binding emails
        # from password providers
        if parse_version(synapse.__version__) <= parse_version("0.99.3"):
            user_id, access_token = await self.account_handler.register(
                localpart=mapped_localpart,
                displayname=name,
            )
        else:
            # If Synapse has support, bind emails
            user_id, access_token = await self.account_handler.register(
                localpart=mapped_localpart,
                displayname=name,
                emails=emails,
            )

        # Store original LDAP localpart in user_external_ids for future reference
        # Only store if we applied mapping (original localpart != mapped localpart)
        if original_localpart and original_localpart != mapped_localpart:
            await self._store_original_localpart(user_id, original_localpart)

        logger.info(
            "Registration based on LDAP data was successful: %s",
            user_id,
        )

        return user_id

    async def _store_original_localpart(self, user_id: str, original_localpart: str) -> None:
        """Store original LDAP localpart in user_external_ids table.

        Args:
            user_id: Full Matrix user ID (e.g., '@u790159:domain.com')
            original_localpart: Original LDAP localpart (e.g., '790159')
        """
        try:
            # Use a consistent auth_provider_id for LDAP original localparts
            auth_provider_id = "ldap_original"

            # First check if user already has an external ID for this auth provider
            # Try to access internal store directly
            existing_ldap_original = None

            if hasattr(self.account_handler, '_store'):
                try:
                    store = self.account_handler._store
                    # Use the exact method name from Synapse store
                    existing_external_ids = await store.get_external_ids_by_user(user_id)

                    for auth_provider, external_id in existing_external_ids:
                        if auth_provider == auth_provider_id:
                            existing_ldap_original = external_id
                            break
                except Exception as e:
                    logger.debug("Could not check existing external IDs via store: %s", e)

            if existing_ldap_original:
                if existing_ldap_original == original_localpart:
                    logger.debug("Original localpart '%s' already stored for user '%s'",
                                original_localpart, user_id)
                    return
                else:
                    logger.info("User '%s' already has different original localpart '%s', not updating to '%s'",
                               user_id, existing_ldap_original, original_localpart)
                    return

            # Store the mapping in user_external_ids table
            await self.account_handler.record_user_external_id(
                auth_provider_id, original_localpart, user_id
            )

            logger.debug("Stored original localpart '%s' for user '%s'",
                        original_localpart, user_id)
        except Exception as e:
            logger.warning("Failed to store original localpart '%s' for user '%s': %s",
                          original_localpart, user_id, e)

    async def _get_original_localpart(self, mapped_localpart: str) -> Optional[str]:
        """Retrieve original LDAP localpart from user_external_ids table.

        Args:
            mapped_localpart: Mapped localpart (e.g., 'u790159')

        Returns:
            Original LDAP localpart if found, None otherwise
        """
        try:
            # Construct the full user_id from mapped localpart
            user_id = self.account_handler.get_qualified_user_id(mapped_localpart)

            # Check if user exists
            if not await self.account_handler.check_user_exists(user_id):
                return None

            # Get external IDs for this user via internal store
            auth_provider_id = "ldap_original"

            if hasattr(self.account_handler, '_store'):
                try:
                    store = self.account_handler._store
                    external_ids = await store.get_external_ids_by_user(user_id)

                    # Look for our stored original localpart
                    for auth_provider, external_id in external_ids:
                        if auth_provider == auth_provider_id:
                            logger.debug("Found original localpart '%s' for mapped localpart '%s'",
                                        external_id, mapped_localpart)
                            return external_id
                except Exception as e:
                    logger.debug("Could not get external IDs via store: %s", e)

            return None
        except Exception as e:
            logger.warning("Failed to retrieve original localpart for '%s': %s",
                          mapped_localpart, e)
            return None

    async def _find_user_by_original_localpart(self, original_localpart: str) -> Optional[str]:
        """Find existing user by original LDAP localpart.

        Uses Synapse's internal store to efficiently find user by external ID.

        Args:
            original_localpart: Original LDAP localpart (e.g., '790159')

        Returns:
            Full Matrix user ID if found, None otherwise
        """
        try:
            auth_provider_id = "ldap_original"

            # Try to access the internal store through ModuleApi
            if hasattr(self.account_handler, '_store'):
                store = self.account_handler._store

                # Implement our own get_user_by_external_id using SQL query
                try:
                    # Use the internal db_pool to query user_external_ids table
                    result = await store.db_pool.simple_select_one_onecol(
                        table="user_external_ids",
                        keyvalues={
                            "auth_provider": auth_provider_id,
                            "external_id": original_localpart,
                        },
                        retcol="user_id",
                        allow_none=True,
                        desc="get_user_by_external_id_ldap",
                    )

                    if result:
                        logger.debug("Found user '%s' by original localpart '%s' using SQL query",
                                    result, original_localpart)
                        return result
                except Exception as e:
                    logger.debug("SQL query failed: %s", e)

            logger.debug("No user found for original localpart '%s'", original_localpart)
            return None

        except Exception as e:
            logger.debug("Error searching for user with original localpart '%s': %s",
                        original_localpart, e)
            return None

    @staticmethod
    def parse_config(config) -> "_LdapConfig":
        # verify config sanity
        _require_keys(
            config,
            [
                "uri",
                "base",
                "attributes",
            ],
        )

        ldap_config = _LdapConfig(
            enabled=config.get("enabled", False),
            mode=LDAPMode.SEARCH
            if config.get("mode", "simple") == "search"
            else LDAPMode.SIMPLE,
            uri=config["uri"],
            start_tls=config.get("start_tls", False),
            tls_options=config.get("tls_options"),
            validate_cert=config.get("validate_cert", True),
            base=config["base"],
            attributes=config["attributes"],
        )

        if "bind_dn" in config:
            ldap_config.mode = LDAPMode.SEARCH
            _require_keys(
                config,
                [
                    "bind_dn",
                ],
            )

            ldap_config.bind_dn = config["bind_dn"]
            if "bind_password" in config:
                ldap_config.bind_password = config["bind_password"]
            elif "bind_password_file" in config:
                with open(config["bind_password_file"], "r") as f:
                    ldap_config.bind_password = f.read().rstrip("\n")
            else:
                raise ValueError(
                    "Either bind_password or bind_password_file must be set!"
                )

        if ldap_config.mode == LDAPMode.SEARCH:
            ldap_config.filter = config.get("filter", None)

        # verify attribute lookup
        _require_keys(
            config["attributes"],
            [
                "uid",
                "name",
                "mail",
            ],
        )

        ldap_config.active_directory = config.get("active_directory", False)
        if ldap_config.active_directory:
            ldap_config.default_domain = config.get("default_domain", None)

        # Parse user_mapping configuration
        user_mapping = config.get("user_mapping")
        if user_mapping:
            if not isinstance(user_mapping, dict):
                raise ValueError("user_mapping must be a dictionary")

            localpart_template = user_mapping.get("localpart_template")
            if localpart_template and not isinstance(localpart_template, str):
                raise ValueError("localpart_template must be a string")

            # Validate template contains {localpart} placeholder
            if localpart_template and "{localpart}" not in localpart_template:
                raise ValueError("localpart_template must contain {localpart} placeholder")

            ldap_config.user_mapping = user_mapping

        if "validate_cert" in config and "tls_options" in config:
            raise Exception(
                "You cannot include both validate_cert and tls_options in the config"
            )

        return ldap_config

    def _get_server(self, get_info: Optional[str] = None) -> ldap3.ServerPool:
        """Constructs ServerPool from configured LDAP URIs

        Args:
            get_info: specifies if the server schema and server
            specific info must be read. Defaults to None.

        Returns:
            Servers grouped in a ServerPool
        """
        return ldap3.ServerPool(
            [
                ldap3.Server(uri, get_info=get_info, tls=self.ldap_tls)
                for uri in self.ldap_uris
            ],
        )

    async def _fetch_root_domain(self) -> str:
        """Fetches root domain from LDAP and saves it to ``self.ldap_root_domain``

        Returns:
            The root domain of Active Directory forest
        """
        if self.ldap_root_domain is not None:
            return self.ldap_root_domain

        self.ldap_root_domain = ""

        if self.ldap_mode != LDAPMode.SEARCH:
            logger.info("Fetching root domain is supported in search mode only")
            return self.ldap_root_domain

        server = self._get_server(get_info=ldap3.DSA)

        if self.ldap_bind_dn is None or self.ldap_bind_password is None:
            result, conn = await self._ldap_simple_bind(
                server=server,
                auth_type=ldap3.ANONYMOUS,
            )
        else:
            result, conn = await self._ldap_simple_bind(
                server=server,
                bind_dn=self.ldap_bind_dn,
                password=self.ldap_bind_password,
            )

        if not result:
            logger.warning("Unable to get root domain due to failed LDAP bind")
            return self.ldap_root_domain

        # conn is present because result is True
        assert conn is not None

        if conn.server.info.other and conn.server.info.other.get(
            "rootDomainNamingContext"
        ):
            # conn.server.info.other["rootDomainNamingContext"][0]
            # is of the form DC=example,DC=org
            self.ldap_root_domain = ".".join(
                [
                    dc.split("=")[1]
                    for dc in conn.server.info.other["rootDomainNamingContext"][
                        0
                    ].split(",")
                    if "=" in dc
                ]
            )
            logger.info('Obtained root domain "%s"', self.ldap_root_domain)

        if not self.ldap_root_domain:
            logger.warning(
                "No valid `rootDomainNamingContext` attribute was found in the RootDSE. "
                "Logging in using short domain name will be unavailable."
            )

        await threads.deferToThread(conn.unbind)

        return self.ldap_root_domain

    async def _ldap_simple_bind(
        self,
        server: ldap3.ServerPool,
        bind_dn: Optional[str] = None,
        password: Optional[str] = None,
        auth_type: str = ldap3.SIMPLE,
    ) -> Tuple[bool, Optional[ldap3.Connection]]:
        """Attempt a simple bind with the credentials given by the user against
        the LDAP server.

        Returns True, LDAP3Connection
            if the bind was successful
        Returns False, None
            if an error occured
        """
        if (bind_dn is None or password is None) and auth_type == ldap3.SIMPLE:
            raise ValueError("Missing bind DN or bind password")

        try:
            # bind with the the local user's ldap credentials
            conn = await threads.deferToThread(
                ldap3.Connection,
                server,
                bind_dn,
                password,
                authentication=auth_type,
                read_only=True,
            )
            logger.debug("Established LDAP connection in simple bind mode: %s", conn)

            if self.ldap_start_tls:
                await threads.deferToThread(conn.open)
                await threads.deferToThread(conn.start_tls)
                logger.debug(
                    "Upgraded LDAP connection in simple bind mode through "
                    "StartTLS: %s",
                    conn,
                )

            if await threads.deferToThread(conn.bind):
                # GOOD: bind okay
                logger.debug("LDAP Bind successful in simple bind mode.")
                return (True, conn)

            # BAD: bind failed
            logger.info(
                "Binding against LDAP failed for '%s' failed: %s",
                bind_dn,
                conn.result["description"],
            )
            await threads.deferToThread(conn.unbind)
            return (False, None)

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning("Error during LDAP authentication: %s", e)
            raise

    async def _ldap_authenticated_search(
        self, server: str, password: str, filters: List[Tuple[str, str]]
    ) -> Tuple[bool, Optional[ldap3.Connection], Any]:
        """Attempt to login with the preconfigured bind_dn and then continue
        searching and filtering within the base_dn.

        Fetches the attributes that correspond to uid/name/mail as defined in
        the config.

        Args:
            server: The LDAP server to connect to.
            password: The user's password.
            filters: A list of tuples of key/value pairs to filter the LDAP
                search by.

        Returns:
            Deferred[tuple[bool, LDAP3Connection, response]]: Returns a 3-tuple
            where first field is whether a *single* entry was found, the second
            is the open connection bound to the found user and the final field
            is the LDAP entry of the found entry. If first field is False then
            second and third field will both be None.
        """

        try:
            if self.ldap_bind_dn is None or self.ldap_bind_password is None:
                result, conn = await self._ldap_simple_bind(
                    server=server,
                    auth_type=ldap3.ANONYMOUS,
                )
            else:
                result, conn = await self._ldap_simple_bind(
                    server=server,
                    bind_dn=self.ldap_bind_dn,
                    password=self.ldap_bind_password,
                )

            if not result:
                return (False, None, None)

            # conn is present because result is True
            assert conn is not None

            # Construct search filter
            query = ""
            for filter in filters:
                query += "({key}={value})".format(
                    key=filter[0],
                    value=filter[1],
                )

            if self.ldap_filter:
                query += self.ldap_filter

            # Create an AND query
            query = "(&{query})".format(
                query=query,
            )

            logger.debug("LDAP search filter: %s", query)
            await threads.deferToThread(
                conn.search,
                search_base=self.ldap_base,
                search_filter=query,
                attributes=[
                    self.ldap_attributes["uid"],
                    self.ldap_attributes["name"],
                    self.ldap_attributes["mail"],
                ],
            )

            responses = [
                response
                for response in conn.response
                if response["type"] == "searchResEntry"
            ]

            if len(responses) == 1:
                # GOOD: found exactly one result
                user_dn = responses[0]["dn"]
                logger.debug("LDAP search found dn: %s", user_dn)

                # unbind and simple bind with user_dn to verify the password
                # Note: do not use rebind(), for some reason it did not verify
                #       the password for me!
                await threads.deferToThread(conn.unbind)
                result, conn = await self._ldap_simple_bind(
                    server=server, bind_dn=user_dn, password=password
                )

                return (result, conn, responses[0])
            else:
                # BAD: found 0 or > 1 results, abort!
                if len(responses) == 0:
                    logger.info("LDAP search returned no results for '%s'", filters)
                else:
                    logger.info(
                        "LDAP search returned too many (%s) results for '%s'",
                        len(responses),
                        filters,
                    )
                await threads.deferToThread(conn.unbind)

                return (False, None, None)

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning("Error during LDAP authentication: %s", e)
            raise

    async def _map_login_to_upn(self, username: str) -> Tuple[str, str, str]:
        """Maps user provided login to Active Directory UPN and local part
        of Matrix ID.

        Args:
            username: The user's login

        Raises:
            ActiveDirectoryUPNException: if username can not be mapped to
            userPrincipalName

        Returns:
            a tuple of:
                - Active Directory login;
                - Active Directory domain; and
                - local part of Matrix ID.
        """
        login = username.lower()
        domain = self.ldap_default_domain

        if "\\" in username:
            (domain, login) = username.lower().rsplit("\\", 1)
            ldap_root_domain = await self._fetch_root_domain()
            if ldap_root_domain and not domain.endswith(ldap_root_domain):
                domain += "." + ldap_root_domain
        elif "/" in username:
            (login, domain) = username.lower().rsplit("/", 1)
        elif not self.ldap_default_domain:
            logger.info(
                'No LDAP separator "/" was found in uid "%s" '
                "and LDAP default domain was not configured.",
                username,
            )
            raise ActiveDirectoryUPNException()

        assert domain is not None

        if self.ldap_default_domain and domain == self.ldap_default_domain.lower():
            localpart = login
        else:
            localpart = login + "/" + domain

        return (login, domain, localpart)


class LdapAuthProviderModule(LdapAuthProvider):
    """
    Wrapper for the LDAP Authentication Provider that supports the new generic module interface,
    rather than the Password Authentication Provider module interface.
    """

    def __init__(self, config, api: "ModuleApi"):
        # The Module API is API-compatible in such a way that it's a drop-in
        # replacement for the account handler, where this module is concerned.
        super().__init__(config, account_handler=api)

        # Register callbacks, since the generic module API requires us to
        # explicitly tell it what callbacks we want.
        api.register_password_auth_provider_callbacks(
            auth_checkers={
                (SUPPORTED_LOGIN_TYPE, SUPPORTED_LOGIN_FIELDS): self.wrapped_check_auth
            },
            check_3pid_auth=self.wrapped_check_3pid_auth,
        )

    async def wrapped_check_auth(
        self, username: str, login_type: str, login_dict: JsonDict
    ) -> Optional[Tuple[str, None]]:
        """
        Wrapper between the old-style `check_auth` interface and the new one.
        """
        result = await self.check_auth(username, login_type, login_dict)
        if result is None:
            return None
        else:
            return result, None

    async def wrapped_check_3pid_auth(
        self, medium: str, address: str, password: str
    ) -> Optional[Tuple[str, None]]:
        """
        Wrapper between the old-style `check_3pid_auth` interface and the new one.
        """
        result = await self.check_3pid_auth(medium, address, password)
        if result is None:
            return None
        else:
            return result, None


def _require_keys(config: Dict[str, Any], required: Iterable[str]) -> None:
    missing = [key for key in required if key not in config]
    if missing:
        raise Exception(
            "LDAP enabled but missing required config values: {}".format(
                ", ".join(missing)
            )
        )
