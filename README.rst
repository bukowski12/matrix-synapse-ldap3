Synapse LDAP Auth Provider
==========================

Allows synapse to use LDAP as a password provider.

This allows users to log in to synapse with their username and password from an
LDAP server. There is also `ma1sd <https://github.com/ma1uta/ma1sd>`_ (3rd party)
that offers more fully-featured integration.

Installation
------------
- Included as standard in the `deb packages <https://matrix-org.github.io/synapse/latest/setup/installation.html#matrixorg-packages>`_ and
  `docker images <https://matrix-org.github.io/synapse/latest/setup/installation.html#docker-images-and-ansible-playbooks>`_ from matrix.org.
- If you installed into a virtualenv:
    - Ensure pip is up-to-date: `pip install -U pip`.
    - Install the LDAP password provider: `pip install matrix-synapse-ldap3`.
- For other installation mechanisms, see the documentation provided by the maintainer.

Usage
-----

Example Synapse configuration:

.. code:: yaml

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
        #user_mapping:
        #   localpart_template: "u{localpart}"  # 12345 → u12345

If you would like to specify more than one LDAP server for HA, you can provide uri parameter with a list.
Default HA strategy of ldap3.ServerPool is employed, so first available server is used.

.. code:: yaml

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

If you would like to enable login/registration via email, or givenName/email
binding upon registration, you need to enable search mode. An example config
in search mode is provided below:

.. code:: yaml

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

Alternatively you can also put the ``bind_password`` of your service user into its
own file to not leak secrets into your configuration:

.. code:: yaml

   modules:
    - module: "ldap_auth_provider.LdapAuthProviderModule"
      config:
        enabled: true
        # all the other options you need
        bind_password_file: "/var/secrets/synapse-ldap-bind-password"

Please note that every trailing ``\n`` in the password file will be stripped automatically.

Active Directory forest support
-------------------------------

If the ``active_directory`` flag is set to ``true``, an Active Directory forest will be
searched for the login details.
In this mode, the user enters their login details in one of the forms:

- ``<login>/<domain>``
- ``<domain>\<login>``

In either case, this will be mapped to the Matrix UID ``<login>/<domain>`` (The 
normal AD domain separators, ``@`` and ``\``, cannot be used in Matrix User Identifiers, so 
``/`` is used instead.)

Let's say you have several domains in the ``example.com`` forest:

.. code:: yaml

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

With this configuration the user can log in with either ``main\someuser``,
``main.example.com\someuser``, ``someuser/main.example.com`` or ``someuser``.

Users of other domains in the ``example.com`` forest can log in with ``domain\login``
or ``login/domain``.

Please note that ``userPrincipalName`` or a similar-looking LDAP attribute in the format
``login@domain`` must be used when the ``active_directory`` option is enabled.

Troubleshooting and Debugging
-----------------------------

``matrix-synapse-ldap3`` logging is included in the Synapse homeserver log
(typically ``homeserver.log``). The LDAP plugin log level can be increased to
``DEBUG`` for troubleshooting and debugging by making the following modifications
to your Synapse server's logging configuration file:

- Set the value for `handlers.file.level` to `DEBUG`:

.. code:: yaml

   handlers:
     file:
       # [...]
       level: DEBUG

- Add the following to the `loggers` section:

.. code:: yaml

   loggers:
      # [...]
      ldap3:
        level: DEBUG
      ldap_auth_provider:
        level: DEBUG

Finally, restart your Synapse server for the changes to take effect:

.. code:: sh

   synctl restart
