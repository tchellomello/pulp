#!/usr/bin/python
#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.


# Python
from datetime import datetime
import logging
import gzip
from optparse import OptionParser
import os
import shutil
import time
import traceback
from urlparse import urlparse

# Pulp
import pulp.server.logs
import pulp.server.util
from pulp.server import constants
from pulp.server import comps_util
from pulp.server import config
from pulp.server import crontab
from pulp.server import updateinfo
from pulp.server.compat import chain
from pulp.server.agent import Agent
from pulp.server.api import repo_sync
from pulp.server.api.base import BaseApi
from pulp.server.api.cdn_connect import CDNConnection
from pulp.server.api.cds import CdsApi
from pulp.server.api.distribution import DistributionApi
from pulp.server.api.errata import ErrataApi
from pulp.server.api.file import FileApi
from pulp.server.api.keystore import KeyStore
from pulp.server.api.package import PackageApi
from pulp.server.async import run_async
from pulp.server.auditing import audit
from pulp.server.db import model
#from pulp.server.db.connection import get_object_db
from pulp.server.event.dispatcher import event
from pulp.server.pexceptions import PulpException
from pulp.server.tasking.task import RepoSyncTask
from pulp.server.api.repo_sync import yum_rhn_progress_callback, \
    local_progress_callback


log = logging.getLogger(__name__)

repo_fields = model.Repo(None, None, None).keys()


class RepoApi(BaseApi):
    """
    API for create/delete/syncing of Repo objects
    """

    def __init__(self):
        BaseApi.__init__(self)
        self.packageapi = PackageApi()
        self.errataapi = ErrataApi()
        self.distroapi = DistributionApi()
        self.cdsapi = CdsApi()
        self.fileapi = FileApi()
        self.localStoragePath = constants.LOCAL_STORAGE
        self.published_path = os.path.join(self.localStoragePath, "published", "repos")
        self.distro_path = os.path.join(self.localStoragePath, "published", "ks")

    @property
    def _indexes(self):
        return ["packages", "packagegroups", "packagegroupcategories"]

    @property
    def _unique_indexes(self):
        return ["id"]

    def _getcollection(self):
        #return get_object_db('repos', self._unique_indexes, self._indexes)
        return model.Repo.get_collection()

    def _validate_schedule(self, sync_schedule):
        '''
        Verifies the sync schedule is in the correct cron syntax, throwing an exception if
        it is not.
        '''
        if sync_schedule:
            item = crontab.CronItem(sync_schedule + ' null') # CronItem expects a command
            if not item.is_valid():
                raise PulpException('Invalid sync schedule specified [%s]' % sync_schedule)

    def _get_existing_repo(self, id, fields=None):
        """
        Protected helper function to look up a repository by id and raise a
        PulpException if it is not found.
        """
        repo = self.repository(id, fields)
        if repo is None:
            raise PulpException("No Repo with id: %s found" % id)
        return repo

    def _hascontent(self, repo):
        """
        Get whether the specified repo has content
        @param repo: A repo.
        @type repo: dict
        @return: True if has content
        @rtype: bool
        """
        try:
            rootdir = pulp.server.util.top_repos_location()
            relativepath = repo['relative_path']
            path = os.path.join(rootdir, relativepath)
            return len(os.listdir(path))
        except:
            return 0

    @audit()
    def clean(self):
        """
        Delete all the Repo objects in the database and remove associated
        files from filesystem.  WARNING: Destructive
        """
        found = self.repositories(fields=["id"])
        for r in found:
            self.delete(r["id"])

    @event(subject='repo.created')
    @audit(params=['id', 'name', 'arch', 'feed'])
    def create(self, id, name, arch, feed=None, symlinks=False, sync_schedule=None,
               cert_data=None, groupid=(), relative_path=None, gpgkeys=(), checksum_type="sha256"):
        """
        Create a new Repository object and return it
        """
        repo = self.repository(id)
        if repo is not None:
            raise PulpException("A Repo with id %s already exists" % id)

        if not model.Repo.is_supported_arch(arch):
            raise PulpException('Architecture must be one of [%s]' % ', '.join(model.Repo.SUPPORTED_ARCHS))
        
        if not model.Repo.is_supported_checksum(checksum_type):
            raise PulpException('Checksum Type must be one of [%s]' % ', '.join(model.Repo.SUPPORTED_CHECKSUMS))

        self._validate_schedule(sync_schedule)

        r = model.Repo(id, name, arch, feed)
        r['sync_schedule'] = sync_schedule
        r['use_symlinks'] = symlinks
        if cert_data:
            cert_files = self._write_certs_to_disk(id, cert_data)
            for key, value in cert_files.items():
                r[key] = value
        if groupid:
            for gid in groupid:
                r['groupid'].append(gid)

        if relative_path is None or relative_path == "":
            if r['source'] is not None :
                if r['source']['type'] == "local":
                    r['relative_path'] = r['id']
                else:
                    # For none product repos, default to repoid
                    url_parse = urlparse(str(r['source']["url"]))
                    r['relative_path'] = url_parse[2] or r['id']
            else:
                r['relative_path'] = r['id']

        else:
            r['relative_path'] = relative_path
        # Remove leading "/", they will interfere with symlink
        # operations for publishing a repository
        r['relative_path'] = r['relative_path'].strip('/')
        r['repomd_xml_path'] = \
                os.path.join(pulp.server.util.top_repos_location(),
                        r['relative_path'], 'repodata/repomd.xml')
        r['checksum_type'] = checksum_type
        if gpgkeys:
            root = pulp.server.util.top_repos_location()
            path = r['relative_path']
            ks = KeyStore(path)
            added = ks.add(gpgkeys)
        self.insert(r)
        if sync_schedule:
            repo_sync.update_schedule(r)
        default_to_publish = \
            config.config.getboolean('repos', 'default_to_published')
        self.publish(r["id"], default_to_publish)
        # refresh repo object from mongo
        r = self.repository(r["id"])
        return r

    @audit(params=['id', 'state'])
    def publish(self, id, state):
        """
        Controls if we publish this repository through Apache.  True means the
        repository will be published, False means it will not be.
        @type id: str
        @param id: repository id
        @type state: boolean
        @param state: True is enable publish, False is disable publish
        """
        repo = self._get_existing_repo(id)
        repo['publish'] = state
        self.update(repo)
        repo = self._get_existing_repo(id)
        try:
            if repo['publish']:
                self._create_published_link(repo)
                if repo['distributionid']:
                    self._create_ks_link(repo)
            else:
                self._delete_published_link(repo)
                if repo['distributionid']:
                    self._delete_ks_link(repo)
            self.update_subscribed(id)
        except Exception, e:
            log.error(e)
            return False
        return True

    def _create_published_link(self, repo):
        if not os.path.isdir(self.published_path):
            os.makedirs(self.published_path)
        source_path = os.path.join(pulp.server.util.top_repos_location(),
                repo["relative_path"])
        link_path = os.path.join(self.published_path, repo["relative_path"])
        pulp.server.util.create_symlinks(source_path, link_path)

    def _delete_published_link(self, repo):
        if repo["relative_path"]:
            link_path = os.path.join(self.published_path, repo["relative_path"])
            if os.path.lexists(link_path):
                # need to use lexists so we will return True even for broken links
                os.unlink(link_path)

    def _clone(self, id, clone_id, clone_name, feed='parent', groupid=None, relative_path=None, progress_callback=None):
        repo = self.repository(id)
        if repo is None:
            raise PulpException("A Repo with id %s does not exist" % id)
        cloned_repo = self.repository(clone_id)
        if cloned_repo is not None:
            raise PulpException("A Repo with id %s exists. Choose a different id." % clone_id)

        REPOS_LOCATION = pulp.server.util.top_repos_location()
        parent_relative_path = "local:file://" + REPOS_LOCATION + "/" + repo["relative_path"]
        cert_data = {}
        if repo['ca'] and repo['cert'] and repo['key']:
            cert_data = {'ca' : open(repo['ca'], "rb").read(),
                         'cert' : open(repo['cert'], "rb").read(),
                         'key'  : open(repo['key'], "rb").read()}
        log.info("Creating repo [%s] cloned from [%s]" % (clone_id, id))
        if feed == 'origin':
            origin_feed = repo['source']['type'] + ":" + repo['source']['url']
            self.create(clone_id, clone_name, repo['arch'], feed=origin_feed, groupid=groupid,
                        relative_path=clone_id, cert_data=cert_data, checksum_type=repo['checksum_type'])
        else:
            self.create(clone_id, clone_name, repo['arch'], feed=parent_relative_path, groupid=groupid,
                        relative_path=relative_path, cert_data=cert_data, checksum_type=repo['checksum_type'])
        # Sync from parent repo
        try:
            self._sync(clone_id, progress_callback=progress_callback)
        except Exception, e:
            log.error(e)
            log.warn("Traceback: %s" % (traceback.format_exc()))
            raise PulpException("Repo cloning of [%s] failed" % id)

        # Update feed type for cloned repo if "origin" or "feedless"
        cloned_repo = self.repository(clone_id)
        if feed == "origin":
            cloned_repo['source'] = repo['source']
        elif feed == "none":
            cloned_repo['source'] = None
        self.update(cloned_repo)

        # Update clone_ids for parent repo
        clone_ids = repo['clone_ids']
        clone_ids.append(clone_id)
        repo['clone_ids'] = clone_ids
        self.update(repo)

        # Update gpg keys from parent repo
        keylist = []
        key_paths = self.listkeys(id)
        for key_path in key_paths:
            key_path = REPOS_LOCATION + key_path
            f = open(key_path)
            fn = os.path.basename(key_path)
            content = f.read()
            keylist.append((fn, content))
            f.close()
        self.addkeys(clone_id, keylist)

    @audit()
    def clone(self, id, clone_id, clone_name, feed='parent', groupid=[], relative_path=None, progress_callback=None, timeout=None):
        """
        Run a repo clone asynchronously.
        """
        task = run_async(self._clone,
                         [id, clone_id, clone_name, feed, groupid, relative_path],
                         {},
                         timeout=timeout)
        if feed in ('feedless', 'parent'):
            task.set_progress('progress_callback', local_progress_callback)
        else:
            task.set_progress('progress_callback', yum_rhn_progress_callback)
        return task

    def _write_certs_to_disk(self, repoid, cert_data):
        CONTENT_CERTS_PATH = config.config.get("repos", "content_cert_location")
        cert_dir = os.path.join(CONTENT_CERTS_PATH, repoid)

        if not os.path.exists(cert_dir):
            os.makedirs(cert_dir)
        cert_files = {}
        for key, value in cert_data.items():
            fname = os.path.join(cert_dir, repoid + "." + key)
            try:
                log.error("storing file %s" % fname)
                f = open(fname, 'w')
                f.write(value)
                f.close()
                cert_files[key] = str(fname)
            except:
                raise PulpException("Error storing certificate file %s " % key)
        return cert_files

    @audit(params=['groupid', 'content_set'])
    def create_product_repo(self, content_set, cert_data, groupid=None, gpg_keys=None):
        """
         Creates a repo associated to a product. Usually through an event raised
         from candlepin
         @param groupid: A product the candidate repo should be associated with.
         @type groupid: str
         @param content_set: a dict of content set labels and relative urls
         @type content_set: dict(<label> : <relative_url>,)
         @param cert_data: a dictionary of ca_cert, cert and key for this product
         @type cert_data: dict(ca : <ca_cert>, cert: <ent_cert>, key : <cert_key>)
         @param gpg_keys: list of keys to be associated with the repo
         @type gpg_keys: list(dict(gpg_key_label : <gpg-key-label>, gpg_key_url : url),)
        """
        if not cert_data or not content_set:
            # Nothing further can be done, exit
            return
        cert_files = self._write_certs_to_disk(groupid, cert_data)
        CDN_URL = config.config.get("repos", "content_url")
        CDN_HOST = urlparse(CDN_URL).hostname
        serv = CDNConnection(CDN_HOST, cacert=cert_files['ca'],
                                     cert=cert_files['cert'], key=cert_files['key'])
        serv.connect()
        repo_info = serv.fetch_listing(content_set)
        gkeys = self._get_gpg_keys(serv, gpg_keys)
        for label, uri in repo_info.items():
            try:
                repo = self.create(label, label, arch=label.split("-")[-1],
                                   feed="yum:" + CDN_URL + '/' + uri,
                                   cert_data=cert_data, groupid=[groupid],
                                   relative_path=uri)
                repo['release'] = label.split("-")[-2]
                self.addkeys(repo['id'], gkeys)
                self.update(repo)
            except:
                log.error("Error creating repo %s for product %s" % (label, groupid))
                continue

        serv.disconnect()

    @audit(params=['groupid', 'content_set'])
    def update_product_repo(self, content_set, cert_data, groupid=None, gpg_keys=[]):
        """
         Creates a repo associated to a product. Usually through an event raised
         from candlepin
         @param groupid: A product the candidate repo should be associated with.
         @type groupid: str
         @param content_set: a dict of content set labels and relative urls
         @type content_set: dict(<label> : <relative_url>,)
         @param cert_data: a dictionary of ca_cert, cert and key for this product
         @type cert_data: dict(ca : <ca_cert>, cert: <ent_cert>, key : <cert_key>)
         @param gpg_keys: list of keys to be associated with the repo
         @type gpg_keys: list(dict(gpg_key_label : <gpg-key-label>, gpg_key_url : url),)
        """
        if not cert_data or not content_set:
            # Nothing further can be done, exit
            return
        cert_files = self._write_certs_to_disk(groupid, cert_data)
        CDN_URL = config.config.get("repos", "content_url")
        CDN_HOST = urlparse(CDN_URL).hostname
        serv = CDNConnection(CDN_HOST, cacert=cert_files['ca'],
                                     cert=cert_files['cert'], key=cert_files['key'])
        serv.connect()
        repo_info = serv.fetch_listing(content_set)
        gkeys = self._get_gpg_keys(serv, gpg_keys)
        for label, uri in repo_info.items():
            try:
                repo = self._get_existing_repo(label)
                repo['feed'] = "yum:" + CDN_URL + '/' + uri
                if cert_data:
                    cert_files = self._write_certs_to_disk(label, cert_data)
                    for key, value in cert_files.items():
                        repo[key] = value
                repo['arch'] = label.split("-")[-1]
                repo['relative_path'] = uri
                repo['groupid'] = [groupid]
                self.addkeys(repo['id'], gkeys)
                self.update(repo)
            except PulpException, pe:
                log.error(pe)
                continue
            except:
                log.error("Error updating repo %s for product %s" % (label, groupid))
                continue

        serv.disconnect()

    def _get_gpg_keys(self, serv, gpg_key_list):
        gpg_keys = []
        for gpgkey in gpg_key_list:
            label = gpgkey['gpg_key_label']
            uri = str(gpgkey['gpg_key_url'])
            try:
                if uri.startswith("file://"):
                    key_path = urlparse(uri).path.encode('ascii', 'ignore')
                    ginfo = open(key_path, "rb").read()
                else:
                    ginfo = serv.fetch_gpgkeys(uri)
                gpg_keys.append((label, ginfo))
            except Exception:
                log.error("Unable to fetch the gpg key info for %s" % uri)
        return gpg_keys

    def delete_product_repo(self, groupid=None):
        """
         delete repos associated to a product. Usually through an event raised
         from candlepin
         @param groupid: A product the candidate repo should be associated with.
         @type groupid: str
        """
        if not groupid:
            # Nothing further can be done, exit
            return

        repos = self.repositories(spec={"groupid" : groupid})
        log.error("List of repos to be deleted %s" % repos)
        for repo in repos:
            try:
                self.delete(repo['id'])
            except:
                log.error("Error deleting repo %s for product %s" % (repo['id'], groupid))
                continue

    @event(subject='repo.deleted')
    @audit()
    def delete(self, id, keep_files=False):
        repo = self._get_existing_repo(id)
        log.info("Delete API call invoked %s" % repo)

        # Ensure the repo is not currently associated with a CDS. If it is, the user
        # will have to explicitly remove that association first. This is to prevent the
        # case where a user needs the repo to be immediately removed from being served,
        # but may forget it's currently on a CDS.
        associated_cds_instances = self.cdsapi.cds_with_repo(id)
        if len(associated_cds_instances) != 0:
            hostnames = [c['hostname'] for c in associated_cds_instances]
            log.error('Attempted to delete repo [%s] but it is associated with CDS instances [%s]' % (id, ', '.join(hostnames)))
            raise PulpException('Repo [%s] cannot be deleted until it is unassociated from the CDS instances [%s]' % (id, ', '.join(hostnames)))

        #update feed of clones of this repo to None unless they point to origin feed
        for clone_id in repo['clone_ids']:
            cloned_repo = self._get_existing_repo(clone_id)
            if cloned_repo['source'] != repo['source']:
                cloned_repo['source'] = None
                self.update(cloned_repo)

        #update clone_ids of its parent repo        
        parent_repos = self.repositories({'clone_ids' : id})
        if len(parent_repos) == 1:
            parent_repo = parent_repos[0]
            clone_ids = parent_repo['clone_ids']
            clone_ids.remove(id)
            parent_repo['clone_ids'] = clone_ids
            self.update(parent_repo)

        self._delete_published_link(repo)
        repo_sync.delete_schedule(repo)

        # delete gpg key links
        path = repo['relative_path']
        ks = KeyStore(path)
        ks.clean(True)

        #remove any distributions
        for distroid in repo['distributionid']:
            self.remove_distribution(repo['id'], distroid)

        #remove files:
        for fileid in repo['files']:
            repos = self.find_repos_by_files(fileid)
            if repo["id"] in repos and len(repos) == 1:
                self.fileapi.delete(fileid, keep_files)
            else:
                log.info("Not deleting %s since it is referenced by these repos: %s" % (fileid, repos))
        #unsubscribe consumers from this repo
        #importing here to bypass circular imports
        from pulp.server.api.consumer import ConsumerApi
        capi = ConsumerApi()
        bound_consumers = capi.findsubscribed(repo['id'])
        for consumer in bound_consumers:
            try:
                log.info("Unsubscribe repoid %s from consumer %s" % (repo['id'], consumer['id']))
                capi.unbind(consumer['id'], repo['id'])
            except:
                log.error("failed to unbind repoid %s from consumer %s moving on.." % \
                          (repo['id'], consumer['id']))
                continue

        repo_location = pulp.server.util.top_repos_location()
        #delete any data associated to this repo
        for field in ['relative_path', 'cert', 'key', 'ca']:
            if field == 'relative_path' and repo[field]:
                fpath = os.path.join(repo_location, repo[field])
            else:
                fpath = repo[field]
            if fpath and os.path.exists(fpath):
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                    else: # os.path.isdir(fpath):
                        shutil.rmtree(fpath)
                    log.info("removing repo files .... %s" % fpath)
                except:
                    #file removal failed
                    log.error("Unable to cleanup file %s " % fpath)
                    continue

        # delete the object
        self.objectdb.remove({'id' : id}, safe=True)

    @event(subject='repo.updated')
    @audit()
    def update(self, repo_data):
        id = repo_data['id']
        repo = self._get_existing_repo(id)
        prevpath = repo.get('relative_path')
        newpath = repo_data.pop('relative_path', None)
        hascontent = self._hascontent(repo)
        for key, value in repo_data.items():
            # primary key
            if key in ('id', '_id'):
                continue
            # feed changed
            if key == 'feed':
                repo[key] = value
                if value:
                    ds = model.RepoSource(value)
                    repo['source'] = ds
                    if not newpath:
                        newpath = urlparse(ds.url)[2]
                continue
            # sync_schedule changed
            if key == 'sync_schedule':
                repo[key] = value
                if value:
                    self._validate_schedule(value)
                    repo_sync.update_schedule(repo)
                else:
                    repo_sync.delete_schedule(repo)
                continue
            if key == 'use_symlinks':
                if hascontent and (value != repo[key]):
                    raise PulpException(
                        "Repository has content, symlinks cannot be changed")
                repo[key] = value
                continue
            # blindly updated only if the key is valid
            if key in repo:
                repo[key] = value
            continue
        # make sure path is relative.
        if newpath:
            newpath = newpath.strip('/')
        else:
            newpath = prevpath
        pathchanged = (prevpath != newpath)
        #
        # After the repo contains content, the relative path may
        # not be changed indirectly (feed) or directly (relativepath)
        #
        if pathchanged:
            if not hascontent:
                rootdir = pulp.server.util.top_repos_location()
                path = os.path.join(rootdir, prevpath)
                if os.path.exists(path):
                    os.rmdir(path)
                repo['relative_path'] = newpath
                path = os.path.join(rootdir, newpath)
                if not os.path.exists(path):
                    os.makedirs(path)
            else:
                raise PulpException(
                    "Repository has content, relative path cannot be changed")
        # store changed object
        self.objectdb.save(repo, safe=True)
        # update subscribers (after) the object has been saved.
        if pathchanged:
            self.update_subscribed(id)
        return repo

    def repositories(self, spec=None, fields=None):
        """
        Return a list of Repositories
        """
        return list(self.objectdb.find(spec=spec, fields=fields))

    def repository(self, id, fields=None):
        """
        Return a single Repository object
        """
        repos = self.repositories({'id': id}, fields)
        if not repos:
            return None
        return repos[0]

    def packages(self, repo_id, **kwargs):
        """
        Return list of Package objects in this Repo
        @type repo_id: str
        @param repo_id: repository id
        @type kwargs: variable keyword arguments accepted
        @param kwargs: keyword arguments will be passed into package lookup query
        @rtype: list
        @return: package objects belonging to this repository
        """
        repo = self._get_existing_repo(repo_id)
        if not kwargs:
            return self.packageapi.packages_by_id(repo["packages"])
        search_dict = {}
        for key in kwargs:
            search_dict[key] = kwargs[key]
        return self.packageapi.packages_by_id(repo["packages"], **search_dict)

    def package_count(self, id):
        """
        Return the number of packages in a repository.
        @type id: str
        @param id: repository id
        @rtype: int
        @return: the number of package in the repository corresponding to id
        """
        return self.repository(id, fields=["package_count"])['package_count']

    def get_package(self, repo_id, name):
        return self.get_packages_by_name(repo_id, name)

    def get_packages_by_id(self, repo_id, pkg_ids):
        """
        Return package objects for the passed in pkg_ids that are in repo_id
        @type repo_id: string
        @param repo_id: repository id
        @type pkg_ids: list of strings
        @param pkg_ids: list of package ids
        """
        repo = self._get_existing_repo(repo_id)
        #Restrict id's to only those that are in this repository
        ids = list(set(pkg_ids).intersection(repo["packages"]))
        return self.packageapi.packages_by_id(ids)

    def get_packages_by_name(self, repo_id, name):
        """
        Return matching Package objects in this Repo
        """
        repo = self._get_existing_repo(repo_id)
        return self.packageapi.packages_by_id(repo["packages"], name=name)

    def get_packages_by_nvrea(self, repo_id, nvreas=[]):
        """
         CHeck if package exists or not in this repo for given nvrea
        """
        log.error('looking up pkg [%s] in repo [%s]' % (nvreas, repo_id))
        #TODO: Potential to make this call quicker and pass more of the checks into mongo
        repo = self._get_existing_repo(repo_id)
        repo_packages = repo['packages']
        pkgs = {}
        for nvrea in nvreas:
            for pkg_id in repo_packages:
                p = self.packageapi.package(pkg_id)
                if not p:
                    continue
                if (nvrea['name'], nvrea['version'], nvrea['release'], nvrea['epoch'], nvrea['arch']) == \
                    (p['name'], p['version'], p['release'], p['epoch'], p['arch']):
                        pkg_repo_path = pulp.server.util.get_repo_package_path(
                                             repo['relative_path'], p['filename'])
                        if os.path.exists(pkg_repo_path):
                            pkgs[p['filename']] = p
        return pkgs

    def get_packages_by_filename(self, repo_id, filenames=[]):
        """
          Return matching Package object in this Repo by filename
        """
        log.info('looking up pkg filename [%s] in repo [%s]' % (filenames, repo_id))
        repo = self._get_existing_repo(repo_id)
        return self.packageapi.packages_by_id(repo["packages"], filename={"$in":filenames})

    def get_packages(self, repo_id, spec={}, pkg_fields=None):
        """
        Generic call to get the packages in a repository that match the given
        specification.
        """
        repo = self._get_existing_repo(repo_id, ['packages'])
        collection = model.Package.get_collection()
        spec['id'] = {'$in': list(repo['packages'])}
        cursor = collection.find(spec=spec, fields=pkg_fields)
        if cursor.count() > 0:
            return list(cursor)
        return []

    @audit()
    def add_package(self, repoid, packageids=[]):
        """
        Adds the passed in package to this repo
        """
        repo = self._get_existing_repo(repoid)
        repo_path = os.path.join(
                pulp.server.util.top_repos_location(), repo['relative_path'])
        if not os.path.exists(repo_path):
            os.makedirs(repo_path)
        for pid in packageids:
            package = self.packageapi.package(pid)
            if package is None:
                log.error("No Package with id: %s found" % pid)
                continue
            nvrea = {'name' : package['name'],
                     'version' : package['version'],
                     'release' : package['release'],
                     'arch'    : package['arch'],
                     'epoch'   : package['epoch'], }
            found = self.get_packages_by_nvrea(repo['id'], [nvrea])
            if found:
                log.error("Package with same NVREA [%s] already exists in repo [%s]"\
                           % (nvrea, repo['id']))
                continue
            self._add_package(repo, package)
            log.info("Added: %s to repo: %s" % (package, repo))
            shared_pkg = pulp.server.util.get_shared_package_path(
                    package['name'], package['version'], package['release'],
		    package['arch'], package["filename"], package['checksum'])
            pkg_repo_path = pulp.server.util.get_repo_package_path(
                    repo['relative_path'], package["filename"])
            if not os.path.exists(pkg_repo_path):
                try:
                    os.symlink(shared_pkg, pkg_repo_path)
                except OSError:
                    log.error("Link %s already exists" % pkg_repo_path)
        pulp.server.util.create_repo(repo_path, checksum_type=repo["checksum_type"])
        self.objectdb.save(repo, safe=True)

    def _add_package(self, repo, p):
        """
        Responsible for properly associating a Package to a Repo
        """
        pkgid = p
        try:
            pkgid = p["id"]
        except:
            # Attempt to access as a SON or a Dictionary, Fall back to a regular package id
            pass
        if pkgid not in repo['packages']:
            repo['packages'].append(pkgid)
            repo['package_count'] = repo['package_count'] + 1

    @audit()
    def remove_package(self, repoid, p):
        """Note: This method does not update repo metadata.
        It is assumed metadata has already been updated.
        """
        return self.remove_packages(repoid, [p])

    def remove_packages(self, repoid, pkgobjs=[]):
        """
         Remove one or more packages from a repository
         Note: This method does not update repo metadata.
         It is assumed metadata has already been updated.
        """
        if not pkgobjs:
            log.debug("remove_packages invoked on %s with no packages" % (repoid))
            # Nothing to perform, return
            return
        repo = self._get_existing_repo(repoid)
        for pkg in pkgobjs:
            # this won't fail even if the package is not in the repo's packages
            #removed_pkg = repo['packages'].pop(pkg['id'], None)
            if pkg['id'] not in repo['packages']:
                log.debug("Attempted to remove a package<%s> that isn't part of repo[%s]" % (pkg["filename"], repoid))
                continue
            repo['packages'].remove(pkg['id'])
            repo['package_count'] = repo['package_count'] - 1
            # Remove package from repo location on file system
            pkg_repo_path = pulp.server.util.get_repo_package_path(
                repo['relative_path'], pkg["filename"])
            if os.path.exists(pkg_repo_path):
                log.debug("Delete package %s at %s" % (pkg["filename"], pkg_repo_path))
                os.remove(pkg_repo_path)
            repos_with_pkg = self.find_repos_by_package(pkg["id"])
            if len(repos_with_pkg) == 1 and repoid in repos_with_pkg:
                # NOTE:  We haven't saved the repo object yet, so mongo still
                # thinks that this pkg is associated to repoid.  Therefore if mongo
                # returns this as the only repo associated we are free to delete this
                # package and update mongo at the end of this loop
                self.packageapi.delete(pkg["id"])
                pkg_packages_path = pulp.server.util.get_shared_package_path(
                    pkg["name"], pkg["version"], pkg["release"], pkg["arch"],
                    pkg["filename"], pkg["checksum"])
                if os.path.exists(pkg_packages_path):
                    log.debug("Delete package %s at %s" % (pkg["filename"], pkg_packages_path))
                    os.remove(pkg_packages_path)
        self.objectdb.save(repo, safe=True)


    def find_repos_by_package(self, pkgid):
        """
        Return repos that contain passed in package id
        @param pkgid: package id
        """
        found = self.objectdb.find({"packages":pkgid}, fields=["id"])
        return [r["id"] for r in found]

    def errata(self, id, types=()):
        """
         Look up all applicable errata for a given repo id
        """
        repo = self._get_existing_repo(id)
        errata = repo['errata']
        if not errata:
            return []
        if types:
            try:
                return [item for type in types for item in errata[type]]
            except KeyError, ke:
                log.debug("Invalid errata type requested :[%s]" % (ke))
                raise PulpException("Invalid errata type requested :[%s]" % (ke))
        return list(chain.from_iterable(errata.values()))

    @audit()
    def add_erratum(self, repoid, erratumid):
        """
        Adds in erratum to this repo
        """
        repo = self._get_existing_repo(repoid)
        self._add_erratum(repo, erratumid)
        self.objectdb.save(repo, safe=True)
        self._update_errata_packages(repoid, [erratumid], action='add')
        updateinfo.generate_updateinfo(repo)

    def add_errata(self, repoid, errataids=()):
        """
         Adds a list of errata to this repo
        """
        repo = self._get_existing_repo(repoid)
        for erratumid in errataids:
            self._add_erratum(repo, erratumid)
        self.objectdb.save(repo, safe=True)
        self._update_errata_packages(repoid, errataids, action='add')
        updateinfo.generate_updateinfo(repo)

    def _update_errata_packages(self, repoid, errataids=[], action=None):
        repo = self._get_existing_repo(repoid)
        addids = []
        rmids = []
        for erratumid in errataids:
            erratum = self.errataapi.erratum(erratumid)
            if erratum is None:
                log.info("No Erratum with id: %s found" % erratumid)
                continue

            for pkg in erratum['pkglist']:
                for pinfo in pkg['packages']:
                    if pinfo['epoch'] in ['None', None]:
                        epoch = '0'
                    else:
                        epoch = pinfo['epoch']
                    epkg = self.packageapi.package_by_ivera(pinfo['name'],
                                                            pinfo['version'],
                                                            epoch,
                                                            pinfo['release'],
                                                            pinfo['arch'])
                    if epkg:
                        addids.append(epkg['id'])
                        rmids.append(epkg)
        if action == 'add':
            self.add_package(repo['id'], addids)
        elif action == 'delete':
            self.remove_packages(repo['id'], rmids)

    def _add_erratum(self, repo, erratumid):
        """
        Responsible for properly associating an Erratum to a Repo
        """
        erratum = self.errataapi.erratum(erratumid)
        if erratum is None:
            raise PulpException("No Erratum with id: %s found" % erratumid)

        errata = repo['errata']
        try:
            if erratum['id'] in errata[erratum['type']]:
                #errata already in repo, continue
                return
        except KeyError:
            errata[erratum['type']] = []

        errata[erratum['type']].append(erratum['id'])


    @audit()
    def delete_erratum(self, repoid, erratumid):
        """
        delete erratum from this repo
        """
        repo = self._get_existing_repo(repoid)
        self._delete_erratum(repo, erratumid)
        self.objectdb.save(repo, safe=True)
        self._update_errata_packages(repoid, [erratumid], action='delete')
        updateinfo.generate_updateinfo(repo)

    def delete_errata(self, repoid, errataids):
        """
        delete list of errata from this repo
        """
        repo = self._get_existing_repo(repoid)
        for erratumid in errataids:
            self._delete_erratum(repo, erratumid)
        self.objectdb.save(repo, safe=True)
        self._update_errata_packages(repoid, errataids, action='delete')
        updateinfo.generate_updateinfo(repo)

    def _delete_erratum(self, repo, erratumid):
        """
        Responsible for properly removing an Erratum from a Repo
        """
        erratum = self.errataapi.erratum(erratumid)
        if erratum is None:
            raise PulpException("No Erratum with id: %s found" % erratumid)
        try:
            curr_errata = repo['errata'][erratum['type']]
            if erratum['id'] not in curr_errata:
                log.debug("Erratum %s Not in repo. Nothing to delete" % erratum['id'])
                return
            del curr_errata[curr_errata.index(erratum['id'])]
            repos = self.find_repos_by_errataid(erratum['id'])
            if repo["id"] in repos and len(repos) == 1:
                self.errataapi.delete(erratum['id'])
            else:
                log.debug("Not deleting %s since it is referenced by these repos: %s" % (erratum["id"], repos))
        except Exception, e:
            raise PulpException("Erratum %s delete failed due to Error: %s" % (erratum['id'], e))

    def find_repos_by_errataid(self, errata_id):
        """
        Return repos that contain passed in errata_id
        """
        ret_val = []
        repos = self.repositories(fields=["id", "errata"])
        for r in repos:
            for e_type in r["errata"]:
                if errata_id in r["errata"][e_type]:
                    ret_val.append(r["id"])
                    break
        return ret_val

    @audit(params=['repoid', 'group_id', 'group_name'])
    def create_packagegroup(self, repoid, group_id, group_name, description):
        """
        Creates a new packagegroup saved in the referenced repo
        @param repoid:
        @param group_id:
        @param group_name:
        @param description:
        @return packagegroup object
        """
        repo = self._get_existing_repo(repoid)
        if not repo:
            raise PulpException("Unable to find repository [%s]" % (repoid))
        if group_id in repo['packagegroups']:
            raise PulpException("Package group %s already exists in repo %s" %
                                (group_id, repoid))
        group = model.PackageGroup(group_id, group_name, description)
        repo["packagegroups"][group_id] = group
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])
        return group

    @audit()
    def delete_packagegroup(self, repoid, groupid):
        """
        Remove a packagegroup from a repo
        @param repoid: repo id
        @param groupid: package group id
        """
        repo = self._get_existing_repo(repoid)
        if groupid not in repo['packagegroups']:
            raise PulpException("Group [%s] does not exist in repo [%s]" % (groupid, repo["id"]))
        if repo['packagegroups'][groupid]["immutable"]:
            raise PulpException("Changes to immutable groups are not supported: %s" % (groupid))
        del repo['packagegroups'][groupid]
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    @audit()
    def update_packagegroup(self, repoid, pg):
        """
        Save the passed in PackageGroup to this repo
        @param repoid: repo id
        @param pg: packagegroup
        """
        repo = self._get_existing_repo(repoid)
        pg_id = pg['id']
        if pg_id in repo['packagegroups']:
            if repo["packagegroups"][pg_id]["immutable"]:
                raise PulpException("Changes to immutable groups are not supported: %s" % (pg["id"]))
        repo['packagegroups'][pg_id] = pg
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    @audit()
    def update_packagegroups(self, repoid, pglist):
        """
        Save the list of passed in PackageGroup objects to this repo
        @param repoid: repo id
        @param pglist: list of packagegroups
        """
        repo = self._get_existing_repo(repoid)
        for item in pglist:
            if item['id'] in repo['packagegroups']:
                if repo['packagegroups'][item["id"]]["immutable"]:
                    raise PulpException("Changes to immutable groups are not supported: %s" % (item["id"]))
            repo['packagegroups'][item['id']] = item
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    def packagegroups(self, id):
        """
        Return list of PackageGroup objects in this Repo
        @param id: repo id
        @return: packagegroup or None
        """
        repo = self._get_existing_repo(id)
        return repo['packagegroups']

    def packagegroup(self, repoid, groupid):
        """
        Return a PackageGroup from this Repo
        @param repoid: repo id
        @param groupid: packagegroup id
        @return: packagegroup or None
        """
        repo = self._get_existing_repo(repoid)
        return repo['packagegroups'].get(groupid, None)


    @audit()
    def add_packages_to_group(self, repoid, groupid, pkg_names=(),
            gtype="default", requires=None):
        """
        @param repoid: repository id
        @param groupid: group id
        @param pkg_names: package names
        @param gtype: OPTIONAL type of package group,
            example "mandatory", "default", "optional", "conditional"
        @param requires: represents the 'requires' field for a 
            conditonal package group entry only needed when 
            gtype is 'conditional'
        We are not restricting package names to packages in the repo.  
        It is possible and acceptable for a package group to refer to packages which
        are not known to the repo or pulp.  The package group will be used on 
        the client and will have access to all repos the client can see.
        """

        repo = self._get_existing_repo(repoid)
        if groupid not in repo['packagegroups']:
            raise PulpException("No PackageGroup with id: %s exists in repo %s"
                                % (groupid, repoid))
        group = repo["packagegroups"][groupid]
        if group["immutable"]:
            raise PulpException("Changes to immutable groups are not supported: %s" % (group["id"]))

        for pkg_name in pkg_names:
            if gtype == "mandatory":
                if pkg_name not in group["mandatory_package_names"]:
                    if pkg_name not in group["mandatory_package_names"]:
                        group["mandatory_package_names"].append(pkg_name)
            elif gtype == "conditional":
                if not requires:
                    raise PulpException("Parameter 'requires' has not been set, it is required by conditional group types")
                group["conditional_package_names"][pkg_name] = requires
            elif gtype == "optional":
                if pkg_name not in group["optional_package_names"]:
                    if pkg_name not in group["optional_package_names"]:
                        group["optional_package_names"].append(pkg_name)
            else:
                if pkg_name not in group["default_package_names"]:
                    if pkg_name not in group["default_package_names"]:
                        group["default_package_names"].append(pkg_name)
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    @audit()
    def delete_package_from_group(self, repoid, groupid, pkg_name, gtype="default"):
        """
        @param repoid: repository id
        @param groupid: group id
        @param pkg_name: package name
        @param gtype: OPTIONAL type of package group,
            example "mandatory", "default", "optional"
        """
        repo = self._get_existing_repo(repoid)
        if groupid not in repo['packagegroups']:
            raise PulpException("No PackageGroup with id: %s exists in repo %s"
                                % (groupid, repoid))
        group = repo["packagegroups"][groupid]
        if group["immutable"]:
            raise PulpException("Changes to immutable groups are not supported: %s" % (group["id"]))

        if gtype == "mandatory":
            if pkg_name in group["mandatory_package_names"]:
                group["mandatory_package_names"].remove(pkg_name)
            else:
                raise PulpException("Package %s not present in package group" % (pkg_name))
        elif gtype == "conditional":
            if pkg_name in group["conditional_package_names"]:
                del group["conditional_package_names"][pkg_name]
            else:
                raise PulpException("Package %s not present in conditional package group" % (pkg_name))
        elif gtype == "optional":
            if pkg_name in group["optional_package_names"]:
                group["optional_package_names"].remove(pkg_name)
            else:
                raise PulpException("Package %s not present in package group" % (pkg_name))
        else:
            if pkg_name in group["default_package_names"]:
                group["default_package_names"].remove(pkg_name)
            else:
                raise PulpException("Package %s not present in package group" % (pkg_name))

        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    @audit(params=['repoid', 'cat_id', 'cat_name'])
    def create_packagegroupcategory(self, repoid, cat_id, cat_name, description):
        """
        Creates a new packagegroupcategory saved in the referenced repo
        @param repoid:
        @param cat_id:
        @param cat_name:
        @param description:
        @return packagegroupcategory object
        """
        repo = self._get_existing_repo(repoid)
        if cat_id in repo['packagegroupcategories']:
            raise PulpException("Package group category %s already exists in repo %s" %
                                (cat_id, repoid))
        cat = model.PackageGroupCategory(cat_id, cat_name, description)
        repo["packagegroupcategories"][cat_id] = cat
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])
        return cat

    @audit()
    def delete_packagegroupcategory(self, repoid, categoryid):
        """
        Remove a packagegroupcategory from a repo
        """
        repo = self._get_existing_repo(repoid)
        if categoryid not in repo['packagegroupcategories']:
            return
        if repo['packagegroupcategories'][categoryid]["immutable"]:
            raise PulpException("Changes to immutable categories are not supported: %s" % (categoryid))
        del repo['packagegroupcategories'][categoryid]
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    @audit()
    def delete_packagegroup_from_category(self, repoid, categoryid, groupid):
        repo = self._get_existing_repo(repoid)
        if categoryid in repo['packagegroupcategories']:
            if repo["packagegroupcategories"][categoryid]["immutable"]:
                raise PulpException(
                        "Changes to immutable categories are not supported: %s" \
                                % (categoryid))
            if groupid not in repo['packagegroupcategories'][categoryid]['packagegroupids']:
                raise PulpException(
                        "Group id [%s] is not in category [%s]" % \
                                (groupid, categoryid))
            repo['packagegroupcategories'][categoryid]['packagegroupids'].remove(groupid)
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    @audit()
    def add_packagegroup_to_category(self, repoid, categoryid, groupid):
        repo = self._get_existing_repo(repoid)
        if categoryid in repo['packagegroupcategories']:
            if repo["packagegroupcategories"][categoryid]["immutable"]:
                raise PulpException(
                        "Changes to immutable categories are not supported: %s" \
                                % (categoryid))
        if groupid not in repo['packagegroupcategories'][categoryid]["packagegroupids"]:
            repo['packagegroupcategories'][categoryid]["packagegroupids"].append(groupid)
            self.update(repo)
            self._update_groups_metadata(repo["id"])

    @audit()
    def update_packagegroupcategory(self, repoid, pgc):
        """
        Save the passed in PackageGroupCategory to this repo
        """
        repo = self._get_existing_repo(repoid)
        if pgc['id'] in repo['packagegroupcategories']:
            if repo["packagegroupcategories"][pgc["id"]]["immutable"]:
                raise PulpException("Changes to immutable categories are not supported: %s" % (pgc["id"]))
        repo['packagegroupcategories'][pgc['id']] = pgc
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    @audit()
    def update_packagegroupcategories(self, repoid, pgclist):
        """
        Save the list of passed in PackageGroupCategory objects to this repo
        """
        repo = self._get_existing_repo(repoid)
        for item in pgclist:
            if item['id'] in repo['packagegroupcategories']:
                if repo["packagegroupcategories"][item["id"]]["immutable"]:
                    raise PulpException("Changes to immutable categories are not supported: %s" % item["id"])
            repo['packagegroupcategories'][item['id']] = item
        self.objectdb.save(repo, safe=True)
        self._update_groups_metadata(repo["id"])

    def packagegroupcategories(self, id):
        """
        Return list of PackageGroupCategory objects in this Repo
        """
        repo = self._get_existing_repo(id)
        return repo['packagegroupcategories']

    def packagegroupcategory(self, repoid, categoryid):
        """
        Return a PackageGroupCategory object from this Repo
        """
        repo = self._get_existing_repo(repoid)
        return repo['packagegroupcategories'].get(categoryid, None)

    def _update_groups_metadata(self, repoid):
        """
        Updates the groups metadata (example: comps.xml) for a given repo
        @param repoid: repo id
        @return: True if metadata was successfully updated, otherwise False
        """
        repo = self._get_existing_repo(repoid)
        try:
            # If the repomd file is not valid, or if we are missingg
            # a group metadata file, no point in continuing. 
            if not os.path.exists(repo["repomd_xml_path"]):
                log.warn("Skipping update of groups metadata since missing repomd file: '%s'" %
                          (repo["repomd_xml_path"]))
                return False
            xml = comps_util.form_comps_xml(repo['packagegroupcategories'],
                repo['packagegroups'])
            if repo["group_xml_path"] == "":
                repo["group_xml_path"] = os.path.dirname(repo["repomd_xml_path"])
                repo["group_xml_path"] = os.path.join(os.path.dirname(repo["repomd_xml_path"]),
                                                      "comps.xml")
                self.update(repo)
            f = open(repo["group_xml_path"], "w")
            f.write(xml.encode("utf-8"))
            f.close()
            if repo["group_gz_xml_path"]:
                gz = gzip.open(repo["group_gz_xml_path"], "wb")
                gz.write(xml.encode("utf-8"))
                gz.close()
            return comps_util.update_repomd_xml_file(repo["repomd_xml_path"],
                repo["group_xml_path"], repo["group_gz_xml_path"])
        except Exception, e:
            log.warn("_update_groups_metadata exception caught: %s" % (e))
            log.warn("Traceback: %s" % (traceback.format_exc()))
            return False

    def get_synchronizer(self, source_type):
        return repo_sync.get_synchronizer(source_type)

    def _sync(self, id, skip_dict={}, progress_callback=None, synchronizer=None):
        """
        Sync a repo from the URL contained in the feed
        """
        repo = self._get_existing_repo(id)
        repo_source = repo['source']
        if not repo_source:
            raise PulpException("This repo is not setup for sync. Please add packages using upload.")
        if not synchronizer:
            synchronizer = repo_sync.get_synchronizer(repo_source["type"])
        synchronizer.set_callback(progress_callback)
        log.info("Sync of %s starting, skip_dict = %s" % (id, skip_dict))
        start_sync_items = time.time()
        sync_packages, sync_errataids = \
                repo_sync.sync(
                    repo,
                    repo_source,
                    skip_dict,
                    progress_callback,
                    synchronizer)
        end_sync_items = time.time()
        log.info("Sync returned %s packages, %s errata in %s seconds" % (len(sync_packages),
            len(sync_errataids), (end_sync_items - start_sync_items)))
        # We need to update the repo object in Mongo to account for
        # package_group info added in sync call
        self.update(repo)
        if not skip_dict.has_key('packages') or skip_dict['packages'] != 1:
            old_pkgs = list(set(repo["packages"]).difference(set(sync_packages.keys())))
            old_pkgs = map(self.packageapi.package, old_pkgs)
            old_pkgs = filter(lambda pkg: pkg["repo_defined"], old_pkgs)
            new_pkgs = list(set(sync_packages.keys()).difference(set(repo["packages"])))
            new_pkgs = map(lambda pkg_id: sync_packages[pkg_id], new_pkgs)
            log.info("%s old packages to process, %s new packages to process" % \
                (len(old_pkgs), len(new_pkgs)))
            synchronizer.progress_callback(step="Removing %s packages" % (len(old_pkgs)))
            # Remove packages that are no longer in source repo
            self.remove_packages(repo["id"], old_pkgs)
            # Refresh repo object since we may have deleted some packages
            repo = self._get_existing_repo(id)
            synchronizer.progress_callback(step="Adding %s new packages" % (len(new_pkgs)))
            for pkg in new_pkgs:
                self._add_package(repo, pkg)
            # Update repo for package additions
            self.update(repo)
        if not skip_dict.has_key('errata') or skip_dict['errata'] != 1:
            # Determine removed errata
            synchronizer.progress_callback(step="Processing Errata")
            log.info("Examining %s errata from repo %s" % (len(self.errata(id)), id))
            repo_errata = self.errata(id)
            old_errata = list(set(repo_errata).difference(set(sync_errataids)))
            new_errata = list(set(sync_errataids).difference(set(repo_errata)))
            log.info("Removing %s old errata from repo %s" % (len(old_errata), id))
            self.delete_errata(id, old_errata)
            # Refresh repo object 
            repo = self._get_existing_repo(id) #repo object must be refreshed
            log.info("Adding %s new errata to repo %s" % (len(new_errata), id))
            for eid in new_errata:
                self._add_erratum(repo, eid)
        repo['last_sync'] = datetime.now().strftime("%s")
        synchronizer.progress_callback(step="Finished")
        self.update(repo)

    @audit()
    def sync(self, id, timeout=None, skip=None):
        """
        Run a repo sync asynchronously.
        """
        repo = self.repository(id)
        task = run_async(self._sync,
                         [id, skip],
                         {},
                         timeout=timeout,
                         task_type=RepoSyncTask)
        if repo['source'] is not None:
            source_type = repo['source']['type']
            if source_type in ('yum', 'rhn'):
                task.set_progress('progress_callback',
                                  yum_rhn_progress_callback)
            elif source_type in ('local'):
                task.set_progress('progress_callback',
                                  local_progress_callback)
            synchronizer = self.get_synchronizer(source_type)
            task.set_synchronizer(synchronizer)
        return task

    def list_syncs(self, id):
        """
        List all the syncs for a given repository.
        """
        return [task
                for task in self.find_async(method='_sync')
                if id in task.args]

    @audit(params=['id', 'keylist'])
    def addkeys(self, id, keylist):
        repo = self._get_existing_repo(id)
        path = repo['relative_path']
        ks = KeyStore(path)
        added = ks.add(keylist)
        log.info('repository (%s), added keys: %s', id, added)
        self.update_subscribed(id)
        return added

    @audit(params=['id', 'keylist'])
    def rmkeys(self, id, keylist):
        repo = self._get_existing_repo(id)
        path = repo['relative_path']
        ks = KeyStore(path)
        deleted = ks.delete(keylist)
        log.info('repository (%s), delete keys: %s', id, deleted)
        self.update_subscribed(id)
        return deleted

    def listkeys(self, id):
        repo = self._get_existing_repo(id)
        path = repo['relative_path']
        ks = KeyStore(path)
        return ks.list()

    def update_subscribed(self, repoid):
        """
        Do an asynchronous RMI to subscribed agents
        to update the .repo file.
        @param repoid: The updated repo ID.
        @type repoid: str
        """
        from pulp.server.api.consumer import ConsumerApi
        capi = ConsumerApi()
        cids = [str(c['id']) for c in capi.findsubscribed(repoid)]
        agent = Agent(cids, async=True)
        repolib = agent.Repo()
        repolib.update()

    def all_schedules(self):
        '''
        For all repositories, returns a mapping of repository name to sync schedule.
        
        @rtype:  dict
        @return: key - repo name, value - sync schedule
        '''
        return dict((r['id'], r['sync_schedule']) for r in self.repositories())

    def add_distribution(self, repoid, distroid):
        '''
         Associate a distribution to a given repo
         @param repoid: The repo ID.
         @param distroid: The distribution ID.
        '''
        repo = self._get_existing_repo(repoid)
        if self.distroapi.distribution(distroid) is None:
            raise PulpException("Distribution ID [%s] does not exist" % distroid)
        repo['distributionid'].append(distroid)
        self.objectdb.save(repo, safe=True)
        if repo['publish']:
            self._create_ks_link(repo)
        log.info("Successfully added distribution %s to repo %s" % (distroid, repoid))

    def remove_distribution(self, repoid, distroid):
        '''
         Delete a distribution from a given repo
         @param repoid: The repo ID.
         @param distroid: The distribution ID.
        '''
        repo = self._get_existing_repo(repoid)
        if distroid in repo['distributionid']:
            del repo['distributionid'][repo['distributionid'].index(distroid)]
            self.objectdb.save(repo, safe=True)
            self.distroapi.delete(distroid)
            self._delete_ks_link(repo)
            log.info("Successfully removed distribution %s from repo %s" % (distroid, repoid))
        else:
            log.error("No Distribution with ID %s associated to this repo" % distroid)

    def _create_ks_link(self, repo):
        if not os.path.isdir(self.distro_path):
            os.mkdir(self.distro_path)
        source_path = os.path.join(pulp.server.util.top_repos_location(),
                repo["relative_path"])
        link_path = os.path.join(self.distro_path, repo["relative_path"])
        log.info("Linking %s" % link_path)
        pulp.server.util.create_symlinks(source_path, link_path)

    def _delete_ks_link(self, repo):
        link_path = os.path.join(self.distro_path, repo["relative_path"])
        log.info("Unlinking %s" % link_path)
        if os.path.lexists(link_path):
            # need to use lexists so we will return True even for broken links
            os.unlink(link_path)

    def list_distributions(self, repoid):
        '''
         List distribution in a given repo
         @param repoid: The repo ID.
         @return list: distribution objects.
        '''
        repo = self._get_existing_repo(repoid)
        distributions = []
        for distro in repo['distributionid']:
            distributions.append(self.distroapi.distribution(distro))
        return distributions

    def get_file_checksums(self, data):
        '''
        Fetch the package checksums and filesizes
        @param data: {"repo_id1": ["file_name", ...], "repo_id2": [], ...}
        @return  {"repo_id1": {"file_name": {'checksum':...},...}, "repo_id2": {..}} 
        '''
        result = {}
        for repoid, filenames in data.items():
            repo = self._get_existing_repo(repoid)
            fchecksum = {}
            for fname in filenames:
                filedata = self.packageapi.package_checksum(fname)
                if filedata:
                    fchecksum[fname] = filedata[0]['checksum']
                else:
                    fchecksum[fname] = None
            result[repoid] = fchecksum
        return result

    @audit()
    def add_file(self, repoid, fileids=[]):
        '''
         Add a file to a repo
         @param repoid: The repo ID.
         @param fileid: file ID.
        '''
        repo = self._get_existing_repo(repoid)
        for fid in fileids:
            fileobj = self.fileapi.file(fid)
            if fileobj is None:
                log.error("File ID [%s] does not exist" % fid)
                continue
            if fid not in repo['files']:
                repo['files'].append(fid)
        self.objectdb.save(repo, safe=True)
        log.info("Successfully added files %s to repo %s" % (fileids, repoid))

    @audit()
    def remove_file(self, repoid, fileids=[]):
        '''
         remove a file from a given repo
         @param repoid: The repo ID.
         @param fileid: file ID.
        '''
        repo = self._get_existing_repo(repoid)
        for fid in fileids:
            fileobj = self.fileapi.file(fid)
            if fileobj is None:
                log.error("File ID [%s] does not exist" % fid)
                continue
            if fid in repo['files']:
                del repo['files'][repo['files'].index(fid)]
            self.objectdb.save(repo, safe=True)
            log.info("Successfully removed file %s from repo %s" % (fileids, repoid))
        else:
            log.error("No file with ID %s associated to this repo" % fileids)
            
    def list_files(self, repoid):
        '''
         List files in a given repo
         @param repoid: The repo ID.
         @return list: file objects.
        '''
        repo = self._get_existing_repo(repoid)
        files = []
        for fileid in repo['files']:
            files.append(self.fileapi.file(fileid))
        return files

    def find_repos_by_files(self, fileid):
        """
        Return repos that contain passed in file id
        @param pkgid: file id
        """
        found = self.objectdb.find({"files":fileid}, fields=["id"])
        return [r["id"] for r in found]

# The crontab entry will call this module, so the following is used to trigger the
# repo sync
if __name__ == '__main__':

    # Need to start logging since this will be called outside of the WSGI application
    pulp.server.logs.start_logging()

    # Currently this option parser is configured to automatically assume repo sync. If
    # further repo-related operations are ever added this will need to be refined, along
    # with the call in repo_sync.py that creates the cron entry that calls this script.
    parser = OptionParser()
    parser.add_option('--repoid', dest='repo_id', action='store')

    options, args = parser.parse_args()

    if options.repo_id:
        log.info('Running scheduled sync for repo [%s]' % options.repo_id)
        repo_api = RepoApi()
        repo_api._sync(options.repo_id)

