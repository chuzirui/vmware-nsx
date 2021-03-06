# Copyright 2016 VMware, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import logging

from vmware_nsx._i18n import _LI
from vmware_nsx.plugins.nsx_v3 import cert_utils
from vmware_nsx.shell.admin.plugins.common import constants
from vmware_nsx.shell.admin.plugins.common import utils as admin_utils
from vmware_nsx.shell.admin.plugins.nsxv3.resources import utils
from vmware_nsx.shell import resources as shell
from vmware_nsxlib.v3 import client_cert
from vmware_nsxlib.v3 import trust_management

from neutron.callbacks import registry
from neutron_lib import context
from neutron_lib import exceptions
from oslo_config import cfg

LOG = logging.getLogger(__name__)

CERT_DEFAULTS = {'key-size': 2048,
                 'sig-alg': 'sha256',
                 'valid-days': 3650,
                 'country': 'US',
                 'state': 'California',
                 'org': 'default org',
                 'unit': 'default unit',
                 'host': 'defaulthost.org'}


def get_nsx_trust_management(**kwargs):
    username, password = None, None
    if kwargs.get('property'):
        properties = admin_utils.parse_multi_keyval_opt(kwargs['property'])
        username = properties.get('user')
        password = properties.get('password')

    nsx_client = utils.get_nsxv3_client(username, password, True)
    nsx_trust = trust_management.NsxLibTrustManagement(nsx_client, {})
    return nsx_trust


def get_certificate_manager(**kwargs):
    storage_driver_type = cfg.CONF.nsx_v3.nsx_client_cert_storage.lower()
    LOG.info(_LI("Certificate storage is %s"), storage_driver_type)
    if storage_driver_type == 'nsx-db':
        storage_driver = cert_utils.DbCertificateStorageDriver(
            context.get_admin_context())
    elif storage_driver_type == 'none':
        storage_driver = cert_utils.DummyCertificateStorageDriver()
    # TODO(annak) - add support for barbican storage driver

    return client_cert.ClientCertificateManager(
            cert_utils.NSX_OPENSTACK_IDENTITY,
            get_nsx_trust_management(**kwargs),
            storage_driver)


@admin_utils.output_header
def generate_cert(resource, event, trigger, **kwargs):
    """Generate self signed client certificate and private key
    """

    if cfg.CONF.nsx_v3.nsx_client_cert_storage.lower() == "none":
        LOG.info(_LI("Generate operation is not supported "
                     "with storage type 'none'"))
        return

    # update cert defaults based on user input
    properties = CERT_DEFAULTS.copy()
    if kwargs.get('property'):
        properties.update(admin_utils.parse_multi_keyval_opt(
            kwargs['property']))

    try:
        prop = 'key-size'
        key_size = int(properties.get(prop))
        prop = 'valid-days'
        valid_for_days = int(properties.get(prop))
    except ValueError:
        LOG.info(_LI("%s property must be a number"), prop)
        return

    signature_alg = properties.get('sig-alg')
    # TODO(annak): use nsxlib constants when they land
    subject = {}
    subject['country'] = properties.get('country')
    subject['state'] = properties.get('state')
    subject['organization'] = properties.get('org')
    subject['unit'] = properties.get('org')
    subject['hostname'] = properties.get('host')

    with get_certificate_manager(**kwargs) as cert:
        if cert.exists():
            LOG.info(_LI("Deleting existing certificate"))
            # Need to delete cert first
            cert.delete()

        try:
            cert.generate(subject, key_size, valid_for_days, signature_alg)
        except exceptions.InvalidInput as e:
            LOG.info(e)
            return

    LOG.info(_LI("Client certificate generated succesfully"))


@admin_utils.output_header
def delete_cert(resource, event, trigger, **kwargs):
    """Delete client certificate and private key """

    with get_certificate_manager(**kwargs) as cert:
        if cfg.CONF.nsx_v3.nsx_client_cert_storage.lower() == "none":
            filename = get_cert_filename(**kwargs)
            if not filename:
                LOG.info(_LI("Please specify file containing the certificate "
                         "using filename property"))
                return
            cert.delete_pem(filename)
        else:
            if not cert.exists():
                LOG.info(_LI("Nothing to clean"))
                return

            cert.delete()
        LOG.info(_LI("Client certificate deleted succesfully"))


@admin_utils.output_header
def show_cert(resource, event, trigger, **kwargs):
    """Show client certificate details """

    with get_certificate_manager(**kwargs) as cert:
        if cert.exists():
            cert_pem, key_pem = cert.get_pem()
            expires_on = cert.expires_on()
            expires_in_days = cert.expires_in_days()
            cert_data = cert.get_subject()
            cert_data['alg'] = cert.get_signature_alg()
            cert_data['key_size'] = cert.get_key_size()
            if expires_in_days >= 0:
                LOG.info(_LI("Client certificate is valid. "
                             "Expires on %(date)s UTC (in %(days)d days)."),
                         {'date': expires_on,
                          'days': expires_in_days})

            else:
                LOG.info(_LI("Client certificate expired on %s."), expires_on)

            LOG.info(_LI("Key Size %(key_size)s, "
                         "Signature Algorithm %(alg)s\n"
                         "Subject: Country %(country)s, State %(state)s, "
                         "Organization %(organization)s, Unit %(unit)s, "
                         "Common Name %(hostname)s"), cert_data)

            LOG.info(cert_pem)
        else:
            LOG.info(_LI("Client certificate is not registered "
                         "in storage"))


def get_cert_filename(**kwargs):
    filename = cfg.CONF.nsx_v3.nsx_client_cert_file
    if kwargs.get('property'):
        properties = admin_utils.parse_multi_keyval_opt(kwargs['property'])
        filename = properties.get('filename', filename)

    if not filename:
        LOG.info(_LI("Please specify file containing the certificate "
                     "using filename property"))
    return filename


@admin_utils.output_header
def import_cert(resource, event, trigger, **kwargs):
    """Import client certificate that was generated externally"""

    if cfg.CONF.nsx_v3.nsx_client_cert_storage.lower() != "none":
        LOG.info(_LI("Import operation is supported "
                     "with storage type 'none' only"))
        return

    with get_certificate_manager(**kwargs) as cert:
        if cert.exists():
            LOG.info(_LI("Deleting existing certificate"))
            cert.delete()

        filename = get_cert_filename(**kwargs)
        if not filename:
            return
        cert.import_pem(filename)

    LOG.info(_LI("Client certificate imported succesfully"))


@admin_utils.output_header
def show_nsx_certs(resource, event, trigger, **kwargs):
    """Show client certificates associated with openstack identity in NSX"""
    nsx_trust = get_nsx_trust_management(**kwargs)

    ids = nsx_trust.get_identities(cert_utils.NSX_OPENSTACK_IDENTITY)
    if not ids:
        LOG.info(_LI("Principal identity %s not found"),
                 cert_utils.NSX_OPENSTACK_IDENTITY)
        return

    LOG.info(_LI("Certificate(s) associated with principal identity %s\n"),
             cert_utils.NSX_OPENSTACK_IDENTITY)

    cert = None
    for identity in ids:
        if 'certificate_id' in identity:
            cert = nsx_trust.get_cert(identity['certificate_id'])

            LOG.info(cert['pem_encoded'])

    if not cert:
        LOG.info(_LI("No certificates found"))


registry.subscribe(generate_cert,
                   constants.CERTIFICATE,
                   shell.Operations.GENERATE.value)

registry.subscribe(show_cert,
                   constants.CERTIFICATE,
                   shell.Operations.SHOW.value)

registry.subscribe(delete_cert,
                   constants.CERTIFICATE,
                   shell.Operations.CLEAN.value)

registry.subscribe(import_cert,
                   constants.CERTIFICATE,
                   shell.Operations.IMPORT.value)

registry.subscribe(show_nsx_certs,
                   constants.CERTIFICATE,
                   shell.Operations.NSX_LIST.value)
