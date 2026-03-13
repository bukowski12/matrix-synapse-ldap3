# Synapse LDAP Auth Provider

Allows synapse to use LDAP as a password provider.

This allows users to log in to synapse with their username and password from an
LDAP server. There is also [ma1sd](https://github.com/ma1uta/ma1sd) (3rd party)
that offers more fully-featured integration.

> [!WARNING]
> Synapse's password provider plugin functionality (which this module relies on)
> is not compatible with [Matrix Authentication
> Service](https://github.com/element-hq/matrix-authentication-service) (MAS), the
> next-gen Matrix auth server.
>
> To use Synapse and MAS together with an LDAP backend, it is recommended to use
> [Dex](https://github.com/dexidp/dex) with [MAS](https://github.com/element-hq/matrix-authentication-service), instead of
> `matrix-synapse-ldap3`. See [the relevant MAS
> documentation](https://element-hq.github.io/matrix-authentication-service/setup/migration.html#map-any-upstream-sso-providers)
> for information on configuring Dex in MAS.

## Installation

- Included as standard in the [deb packages](https://matrix-org.github.io/synapse/latest/setup/installation.html#matrixorg-packages) and
  [docker images](https://matrix-org.github.io/synapse/latest/setup/installation.html#docker-images-and-ansible-playbooks) from matrix.org.
- If you installed into a virtualenv:
    - Ensure pip is up-to-date: `pip install -U pip`.
    - Install the LDAP password provider: `pip install matrix-synapse-ldap3`.
- For other installation mechanisms, see the documentation provided by the maintainer.

## Usage

Example Synapse configuration:

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        uri: "ldap://ldap.example.com:389"
        start_tls: true
        base: "ou=users,dc=example,dc=com"
        attributes:
           uid: "cn"
           mail: "mail"
           name: "givenName"
        #bind_dn:
        #bind_password:
        #filter: "(objectClass=posixAccount)"
        # Additional options for TLS, can be any key from https://ldap3.readthedocs.io/en/latest/ssltls.html#the-tls-object
        #tls_options:
        #  validate: true
        #  local_certificate_file: foo.crt
        #  local_private_key_file: bar.pem
        #  local_private_key_password: secret
```

If you would like to specify more than one LDAP server for HA, you can provide uri parameter with a list.
Default HA strategy of ldap3.ServerPool is employed, so first available server is used.

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        uri:
           - "ldap://ldap1.example.com:389"
           - "ldap://ldap2.example.com:389"
        start_tls: true
        base: "ou=users,dc=example,dc=com"
        attributes:
           uid: "cn"
           mail: "email"
           name: "givenName"
        #bind_dn:
        #bind_password:
        #filter: "(objectClass=posixAccount)"
        #tls_options:
        #  validate: true
        #  local_certificate_file: foo.crt
        #  local_private_key_file: bar.pem
        #  local_private_key_password: secret
```

If you would like to enable login/registration via email, or givenName/email
binding upon registration, you need to enable search mode. An example config
in search mode is provided below:

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        mode: "search"
        uri: "ldap://ldap.example.com:389"
        start_tls: true
        base: "ou=users,dc=example,dc=com"
        attributes:
           uid: "cn"
           mail: "mail"
           name: "givenName"
        # Search auth if anonymous search not enabled
        bind_dn: "cn=hacker,ou=svcaccts,dc=example,dc=com"
        bind_password: "ch33kym0nk3y"
        #filter: "(objectClass=posixAccount)"
        #tls_options:
        #  validate: true
        #  local_certificate_file: foo.crt
        #  local_private_key_file: bar.pem
        #  local_private_key_password: secret
```

Alternatively you can also put the `bind_password` of your service user into its
own file to not leak secrets into your configuration:

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        # all the other options you need
        bind_password_file: "/var/secrets/synapse-ldap-bind-password"
```

Please note that every trailing `\n` in the password file will be stripped automatically.


## User Mapping

The `user_mapping` option allows you to transform LDAP user identifiers into Matrix user identifiers
using a customizable template. The template currently supports only the `{localpart}` placeholder,
which is the local part derived from LDAP. This is useful, for example, when you have numeric IDs in
LDAP, because Synapse does not accept purely numeric usernames since they are reserved for guest accounts.

### Configuration

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        uri: "ldap://ldap.example.com:389"
        start_tls: true
        base: "ou=users,dc=example,dc=com"
        attributes:
           uid: "cn"
           mail: "mail"
           name: "givenName"
        bind_dn: "cn=hacker,ou=svcaccts,dc=example,dc=com"
        bind_password: "ch33kym0nk3y"
        # User mapping configuration
        user_mapping:
          localpart_template: "u{localpart}"
```

### How it works

When `user_mapping` is configured with a `localpart_template`:

1. **LDAP Authentication**: User authenticates against LDAP with their original LDAP username
2. **Template Application**: The LDAP username is transformed using the template
   - Example: LDAP username `123456` + template `u{localpart}` → Matrix user `@u123456:domain.com`
3. **Matrix Registration/Login**: The user's Matrix account uses the transformed localpart
4. **Storage**: The original LDAP localpart is stored in the `user_external_ids` table for future reference

### Example scenarios

**Scenario 1: Prefix usernames with department code**
```yaml
user_mapping:
  localpart_template: "emp_{localpart}"
# LDAP user "john.smith" → Matrix user "@emp_john.smith:example.com"
```

**Scenario 2: Prefix numeric employee IDs**
```yaml
user_mapping:
  localpart_template: "u{localpart}"
# LDAP user "123456" → Matrix user "@u123456:example.com"
```

### Simple vs search mode, and attribute mapping

The module behaves quite differently depending on the configured `mode`:

- If `mode` is omitted (or set to `simple`), the module simply builds a DN from
  `attributes.uid`, binds as the authenticating user, and stops there. No LDAP
  search is performed, meaning `attributes.name` and `attributes.mail` are never
  queried. When a Matrix user is created in this mode their display name is the
  username they logged in with and their email address is left blank.
- To fetch attribute values from LDAP you **must** run in `mode: search`. You can
  optionally supply `bind_dn`/`bind_password` so the module performs the search
  with a service account. If they are omitted, an anonymous bind is attempted
  and succeeds only if your LDAP server allows anonymous reads.

Also note that attribute data (`name`, `mail`) is fetched only when a Matrix
user is created. During each authentication, the module re-checks LDAP
credentials, but existing Matrix accounts keep the profile data stored in
Synapse. Therefore logging in again will not refresh the display name or email
address.


## Room Mapping

The `room_mapping` feature allows you to automatically manage Matrix room memberships based on LDAP group membership. When a user authenticates, the module checks their LDAP group membership and automatically adds them to or removes them from configured Matrix rooms.

### Configuration

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        mode: "search"
        uri: "ldap://ldap.example.com:389"
        start_tls: true
        base: "ou=users,dc=example,dc=com"
        attributes:
           uid: "cn"
           mail: "mail"
           name: "givenName"
        bind_dn: "cn=hacker,ou=svcaccts,dc=example,dc=com"
        bind_password: "ch33kym0nk3y"
        # Room mapping configuration
        room_mapping:
          - cn: "CN=Developers,OU=Groups,DC=example,DC=com"
            rooms:
              - "#dev-team:example.com"
              - "#general:example.com"
          - cn: "CN=Admins,OU=Groups,DC=example,DC=com"
            rooms: "#admin-room:example.com"
        # Optional: User to invite on behalf of (must have permission to invite)
        room_inviter: "@admin:example.com"
        # Optional: Enable nested groups support (Active Directory) - default: true
        nested_groups: true
```

### How it works

1. **Authentication**: When a user authenticates (login or registration), the module queries their LDAP group membership
2. **Group Matching**: The module checks which configured groups (from `room_mapping`) the user belongs to
3. **Room Synchronization**:
   - **Add to rooms**: If the user is a member of an LDAP group, they are automatically added to the corresponding Matrix rooms
   - **Remove from rooms**: If the user is no longer a member of an LDAP group, they are automatically removed from the corresponding Matrix rooms
4. **Nested Groups**: When `nested_groups: true` (default), the module supports Active Directory nested group membership using the LDAP extensible match filter (OID 1.2.840.113556.1.4.1941)

### Configuration options

- **`room_mapping`** (list, optional): List of LDAP group to Matrix room mappings
  - **`cn`** (string, required): Full Distinguished Name (DN) of the LDAP group
  - **`rooms`** (string or list, required): Matrix room alias(es) or room ID(s) to manage
    - Room aliases must start with `#` (e.g., `#room:example.com`)
    - Room IDs must start with `!` (e.g., `!roomid:example.com`)
- **`room_inviter`** (string, optional): Matrix user ID to use for inviting users to rooms
  - Must be a valid Matrix user ID starting with `@` (e.g., `@admin:example.com`)
  - This user must have permission to invite users to the configured rooms
  - If not specified, the module will attempt to join users directly
- **`nested_groups`** (boolean, optional, default: `true`): Enable support for nested groups in Active Directory
  - When `true`, uses LDAP extensible match filter for transitive group membership
  - When `false`, only checks direct group membership

### Example scenarios

**Scenario 1: Department-based room access**
```yaml
room_mapping:
  - cn: "CN=Engineering,OU=Departments,DC=company,DC=com"
    rooms:
      - "#engineering:company.com"
      - "#all-staff:company.com"
  - cn: "CN=Sales,OU=Departments,DC=company,DC=com"
    rooms:
      - "#sales:company.com"
      - "#all-staff:company.com"
```

**Scenario 2: Role-based access with nested groups**
```yaml
room_mapping:
  - cn: "CN=Admins,OU=Groups,DC=company,DC=com"
    rooms: "#admin-room:company.com"
  - cn: "CN=Developers,OU=Groups,DC=company,DC=com"
    rooms:
      - "#dev-team:company.com"
      - "#code-reviews:company.com"
nested_groups: true  # Supports nested group membership
room_inviter: "@bot:company.com"
```

### Important notes

- Room synchronization happens during authentication (login) and user registration
- The module only checks groups that are configured in `room_mapping`, not all LDAP groups
- For Active Directory, `nested_groups: true` enables efficient nested group checking using native LDAP filters
- The `room_inviter` user must exist and have appropriate permissions in the target rooms
- Room aliases are resolved to room IDs automatically by Synapse

## Active Directory forest support

If the ``active_directory`` flag is set to `true`, an Active Directory forest will be
searched for the login details.
In this mode, the user enters their login details in one of the forms:

- `<login>/<domain>`
- `<domain>\<login>`

In either case, this will be mapped to the Matrix UID `<login>/<domain>` (The
normal AD domain separators, `@` and `\`, cannot be used in Matrix User Identifiers, so
`/` is used instead.)

Let's say you have several domains in the `example.com` forest:

```yaml
   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        mode: "search"
        uri: "ldap://main.example.com:389"
        base: "dc=example,dc=com"
        # Must be true for this feature to work
        active_directory: true
        # Optional. Users from this domain may log in without specifying the domain part
        default_domain: main.example.com
        attributes:
           uid: "userPrincipalName"
           mail: "mail"
           name: "givenName"
        bind_dn: "cn=hacker,ou=svcaccts,dc=example,dc=com"
        bind_password: "ch33kym0nk3y"
```

With this configuration the user can log in with either `main\someuser`,
`main.example.com\someuser`, `someuser/main.example.com` or `someuser`.

Users of other domains in the `example.com` forest can log in with `domain\login`
or `login/domain`.

Please note that `userPrincipalName` or a similar-looking LDAP attribute in the format
`login@domain` must be used when the `active_directory` option is enabled.

## Troubleshooting and Debugging

`matrix-synapse-ldap3` logging is included in the Synapse homeserver log
(typically `homeserver.log`). The LDAP plugin log level can be increased to
`DEBUG` for troubleshooting and debugging by making the following modifications
to your Synapse server's logging configuration file:

- Set the value for `handlers.file.level` to `DEBUG`:

```yaml
   handlers:
     file:
       # [...]
       level: DEBUG
```

- Add the following to the `loggers` section:

```yaml
   loggers:
      # [...]
      ldap3:
        level: DEBUG
      ldap_auth_provider:
        level: DEBUG
```

Finally, restart your Synapse server for the changes to take effect:

```shell
synctl restart
```
