# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fs.errors

from pyramid.httpexceptions import HTTPMovedPermanently, HTTPNotFound
from pyramid.response import FileIter, Response
from pyramid.view import view_config
from sqlalchemy.orm.exc import NoResultFound

from warehouse.accounts.models import User
from warehouse.cache.http import cache_control
from warehouse.cache.origin import origin_cache
from warehouse.packaging.interfaces import IDownloadStatService
from warehouse.packaging.models import Release, File, Role


@view_config(
    route_name="packaging.project",
    renderer="packaging/detail.html",
    decorator=[
        cache_control(1 * 24 * 60 * 60),  # 1 day
        origin_cache(7 * 24 * 60 * 60),   # 7 days
    ],
)
def project_detail(project, request):
    if project.name != request.matchdict.get("name", project.name):
        return HTTPMovedPermanently(
            request.current_route_url(name=project.name),
        )

    try:
        release = project.releases.order_by(
            Release._pypi_ordering.desc()
        ).limit(1).one()
    except NoResultFound:
        raise HTTPNotFound from None

    return release_detail(release, request)


@view_config(
    route_name="packaging.release",
    renderer="packaging/detail.html",
    decorator=[
        cache_control(7 * 24 * 60 * 60),  # 7 days
        origin_cache(30 * 24 * 60 * 60),  # 30 days
    ],
)
def release_detail(release, request):
    project = release.project

    if project.name != request.matchdict.get("name", project.name):
        return HTTPMovedPermanently(
            request.current_route_url(name=project.name),
        )

    # Get all of the registered versions for this Project, in order of newest
    # to oldest.
    all_releases = (
        project.releases
        .with_entities(Release.version, Release.created)
        .order_by(Release._pypi_ordering.desc())
        .all()
    )

    # Get all of the maintainers for this project.
    maintainers = [
        r.user
        for r in (
            request.db.query(Role)
            .join(User)
            .filter(Role.project == project)
            .distinct(User.username)
            .order_by(User.username)
            .all()
        )
    ]

    stats_svc = request.find_service(IDownloadStatService)

    return {
        "project": project,
        "release": release,
        "files": release.files.all(),
        "all_releases": all_releases,
        "maintainers": maintainers,
        "download_stats": {
            "daily": stats_svc.get_daily_stats(project.name),
            "weekly": stats_svc.get_weekly_stats(project.name),
            "monthly": stats_svc.get_monthly_stats(project.name),
        },
    }


@view_config(
    route_name="packaging.file",
    decorator=[
        cache_control(365 * 24 * 60 * 60),  # 1 year
    ],
)
def packages(request):
    # The amount of logic that we can do in this view is very limited, this
    # view needs to be able to be handled by Fastly directly hitting S3 instead
    # of actually hitting this view. This more or less means that we're limited
    # to just serving the actual file.

    # Grab the path of the file that we're attempting to serve
    path = request.matchdict["path"]

    # We need to look up the File that is associated with this path, either the
    # package path or the pgp path. If that doesn't exist then we'll bail out
    # early with a 404.
    try:
        file_ = (
            request.db.query(File)
                      .filter((File.path == path) | (File.pgp_path == path))
                      .one()
        )
    except NoResultFound:
        raise HTTPNotFound from None

    # If this request is for a PGP signature, and the file doesn't have a PGP
    # signature, then we can go ahead and 404 now before hitting the file
    # storage.
    if path == file_.pgp_path and not file_.has_pgp_signature:
        raise HTTPNotFound

    # Try to open the file, streaming if possible, and if this file doesn't
    # exist then we'll return a 404 error. However we'll log an error because
    # if the database thinks we have a file, then a file should exist here.
    try:
        # TODO: We need to use mode="rb" here because this is a binary file
        #       and we don't want Python to attempt to decode it. However S3FS
        #       checks explicitly for mode="r-" to support streaming access.
        #       We need to get S3FS so that it support rb- as well as r-.
        f = request.registry["filesystems"]["packages"].open(path, mode="rb")
    except fs.errors.ResourceNotFoundError:
        # TODO: Log an error here, this file doesn't exists for some reason,
        #       but it should because the database thinks it should.
        raise HTTPNotFound from None

    # If the path we're accessing is the path for the package itself, as
    # opposed to the path for the signature, then we can include a
    # Content-Length header.
    content_length = None
    if path == file_.path:
        content_length = file_.size

    return Response(
        # If we have a wsgi.file_wrapper, we'll want to use that so that, if
        # possible, this will use an optimized method of sending. Otherwise
        # we'll just use Pyramid's FileIter as a fallback.
        app_iter=request.environ.get("wsgi.file_wrapper", FileIter)(f),
        # We use application/octet-stream instead of something nicer because
        # different HTTP libraries will treat different combinations of
        # Content-Type and Content-Encoding differently. The only thing that
        # works sanely across all things without having something in the middle
        # decide it can decompress the result to "help" the end user is with
        # Content-Type: applicaton/octet-stream and no Content-Encoding.
        content_type="application/octet-stream",
        content_encoding=None,
        # We need to specify an ETag for this response. Since ETags compared
        # between URLs have no meaning (and thus, is safe for two URLs to share
        # the same ETag) we will just use the MD5 hash of the package as our
        # ETag.
        etag=file_.md5_digest,
        # Similarly to the ETag header, we'll just use the date that the file
        # was uploaded as the Last-Modified header.
        last_modified=file_.upload_time,
        # If we have a Content-Length, we'll go ahead and use it here to
        # hopefully enable the server and clients alike to be smarter about how
        # they handle downloading this response.
        content_length=content_length,
    )
