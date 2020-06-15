"""
s3path provides a Pythonic API to S3 by wrapping boto3 with pathlib interface
"""
from posix import stat_result
from contextlib import suppress
from collections import namedtuple
from tempfile import NamedTemporaryFile
from functools import wraps, partial, lru_cache
from pathlib import _PosixFlavour, _Accessor, PurePath, Path
from io import RawIOBase, DEFAULT_BUFFER_SIZE, UnsupportedOperation
import sys

try:
    import boto3
    from botocore.exceptions import ClientError, WaiterError
    from botocore.response import StreamingBody
    from botocore.docs.docstring import LazyLoadedDocstring
except ImportError:
    boto3 = None
    ClientError = Exception
    StreamingBody = object
    LazyLoadedDocstring = type(None)

__version__ = '0.2.101'
__all__ = (
    'register_configuration_parameter',
    'S3Path',
    'PureS3Path',
    'StatResult',
    'S3DirEntry',
    'S3KeyWritableFileObject',
    'S3KeyReadableFileObject',
)

_SUPPORTED_OPEN_MODES = {'r', 'br', 'rb', 'tr', 'rt', 'w', 'wb', 'bw', 'wt', 'tw'}


class _S3Flavour(_PosixFlavour):
    is_supported = bool(boto3)

    def parse_parts(self, parts):
        drv, root, parsed = super().parse_parts(parts)
        for part in parsed[1:]:
            if part == '..':
                index = parsed.index(part)
                parsed.pop(index - 1)
                parsed.remove(part)
        return drv, root, parsed

    def make_uri(self, path):
        uri = super().make_uri(path)
        return uri.replace('file:///', 's3://')


class _S3ConfigurationMap(dict):
    def __missing__(self, path):
        for parent in path.parents:
            if parent in self:
                return self[parent]
        return self.setdefault(Path('/'), {})


class _S3Scandir:
    def __init__(self, *, S3_accessor, path):
        self._S3_accessor = S3_accessor
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return

    def __iter__(self):
        bucket_name = self._S3_accessor.bucket_name(self._path.bucket)
        if not bucket_name:
            for bucket in self._S3_accessor.s3.buckets.all():
                yield S3DirEntry(bucket.name, is_dir=True)
            return
        bucket = self._S3_accessor.s3.Bucket(bucket_name)
        sep = self._path._flavour.sep

        kwargs = {
            'Bucket': bucket.name,
            'Prefix': self._S3_accessor.generate_prefix(self._path),
            'Delimiter': sep}

        continuation_token = None
        while True:
            if continuation_token:
                kwargs['ContinuationToken'] = continuation_token
            response = bucket.meta.client.list_objects_v2(**kwargs)
            for folder in response.get('CommonPrefixes', ()):
                prefix = folder['Prefix']
                full_name = prefix[:-1] if prefix.endswith(sep) else prefix
                name = full_name.split(sep)[-1]
                yield S3DirEntry(name, is_dir=True)
            for file in response.get('Contents', ()):
                name = file['Key'].split(sep)[-1]
                is_symlink = self._path.joinpath(name).is_symlink()
                if int(file['Size']) == 0 and is_symlink:
                    target = self._S3_accessor._get_object_summary(
                        bucket.name,
                        name,
                        follow_symlinks=True,
                        ignore_errors=False
                    )
                    is_dir = self._S3_accessor.is_dir(target.key)
                    if is_dir:
                        yield S3DirEntry(target.key, is_dir=True)
                    else:
                        yield S3DirEntry(
                            name=target.key,
                            is_dir=is_dir,
                            size=target.size,
                            last_modified=target.last_modified,
                            is_symlink=False,
                        )
                else:
                    yield S3DirEntry(
                        name=name,
                        is_dir=False,
                        size=file['Size'],
                        last_modified=file['LastModified'],
                        is_symlink=is_symlink,
                    )
            if not response.get('IsTruncated'):
                break
            continuation_token = response.get('NextContinuationToken')


class _S3Accessor(_Accessor):
    """
    An accessor implements a particular (system-specific or not)
    way of accessing paths on the filesystem.

    In this case this will access AWS S3 service
    """

    def __init__(self, **kwargs):
        if boto3 is not None:
            self.s3 = boto3.resource('s3', **kwargs)
        self.configuration_map = _S3ConfigurationMap()

    def _wait_for_object_summary(self, object_summary, delay=5, max_attempts=5):
        waiter = object_summary.meta.client.get_waiter("object_exists")
        try:
            waiter.wait(
                Bucket=object_summary.bucket_name,
                Key=object_summary.key,
                WaiterConfig={
                    'Delay': delay,
                    'MaxAttempts': max_attempts,
                }
            )
        except WaiterError:
            raise FileNotFoundError(
                "/{0}/{1}".format(object_summary.bucket_name, object_summary.key)
            )
        return object_summary

    def _get_object_summary(
        self,
        bucket,
        key,
        follow_symlinks=False,
        ignore_errors=False,
    ):
        base_summary = self.s3.ObjectSummary(bucket, key)
        if not follow_symlinks:
            return base_summary
        try:
            symlink_target = base_summary.Object().website_redirect_location
        except ClientError:
            try:
                self._wait_for_object_summary(base_summary)
            except FileNotFoundError:
                # ignore_errors allows us to create or modify non-existent symlinks
                if not ignore_errors:
                    raise
        if not symlink_target and not (
            base_summary.meta and getattr(base_summary.meta, "get", None)
        ):
            symlink_target = None
        elif not symlink_target:
            symlink_target = base_summary.meta.get(
                "x-amz-website-redirect-location", None
            )
        if symlink_target is None:
            return base_summary
        target_path = S3Path(symlink_target)
        target_bucket = self.bucket_name(target_path.bucket)
        target_key = str(target_path.key)
        return self._get_object_summary(
            target_bucket,
            target_key,
            follow_symlinks=follow_symlinks,
            ignore_errors=ignore_errors,
        )

    def stat(self, path):
        object_summery = self._get_object_summary(
            self.bucket_name(path.bucket), str(path.key)
        )
        return StatResult(
            size=object_summery.size,
            last_modified=object_summery.last_modified,
        )

    def is_dir(self, path):
        if str(path) == path.root:
            return True
        bucket = self.s3.Bucket(self.bucket_name(path.bucket))
        return any(bucket.objects.filter(Prefix=self.generate_prefix(path)))

    def is_symlink(self, path):
        bucket_name = self.bucket_name(path.bucket)
        key_name = str(path.key)
        object_summary = self.s3.ObjectSummary(bucket_name, key_name)
        object_inst = object_summary.Object()
        redirect_location = None
        try:
            if path.exists() and path.is_dir():
                return False
        except ClientError:
            raise FileNotFoundError(str(path))
        try:
            redirect_location = object_inst.website_redirect_location
        except ClientError:
            self._wait_for_object_summary(object_summary)
        if redirect_location is not None and object_summary.size == 0:
            return True
        if object_summary.meta and getattr(object_summary.meta, "get", None):
            redirect_location = object_summary.meta.get(
                "x-amz-website-redirect-location", None
            )
        else:
            redirect_location = None
        return redirect_location is not None

    def exists(self, path):
        bucket_name = self.bucket_name(path.bucket)
        if not bucket_name:
            return any(self.s3.buckets.all())
        if not path.key:
            return self.s3.Bucket(bucket_name) in self.s3.buckets.all()
        bucket = self.s3.Bucket(bucket_name)
        key_name = str(path.key)
        for object in bucket.objects.filter(Prefix=key_name):
            if object.key == key_name:
                return True
            if object.key.startswith(key_name + path._flavour.sep):
                return True
        return False

    def scandir(self, path):
        return _S3Scandir(S3_accessor=self, path=path)

    def listdir(self, path):
        with self.scandir(path) as scandir_iter:
            return [entry.name for entry in scandir_iter]

    def open(self, path, *, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        bucket_name = self.bucket_name(path.bucket)
        key_name = str(path.key)
        # We want to follow symlinks when reading file contents, in case a file points
        # off somewhere else -- this way we know we will read the contents of the
        # target
        follow_symlinks = True
        # We don't want to ignore errors when reading, since files that don't exist
        # aren't readable
        ignore_errors = False
        if "w" in mode:
            # We will ignore errors when writing because it's expected that files
            # won't yet exist
            ignore_errors = True
            # We won't follow symlinks when writing, since for example we first have to
            # write a blank value, or we sometimes need to delete the non-referenced
            # object without touching the target of the link
            follow_symlinks = False
        object_summery = self._get_object_summary(
            bucket_name,
            key_name,
            follow_symlinks=follow_symlinks,
            ignore_errors=ignore_errors,
        )
        file_object = S3KeyReadableFileObject if 'r' in mode else S3KeyWritableFileObject
        return file_object(
            object_summery,
            path=path,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline)

    def owner(self, path):
        bucket_name = self.bucket_name(path.bucket)
        key_name = str(path.key)
        object_summery = self.s3.ObjectSummary(bucket_name, key_name)
        # return object_summery.owner['DisplayName']
        # This is a hack till boto3 resolve this issue:
        # https://github.com/boto/boto3/issues/1950
        # todo: need to clean up
        responce = object_summery.meta.client.list_objects_v2(
            Bucket=object_summery.bucket_name,
            Prefix=object_summery.key,
            FetchOwner=True)
        return responce['Contents'][0]['Owner']['DisplayName']

    def rename(self, path, target):
        source_bucket_name = self.bucket_name(path.bucket)
        source_key_name = str(path.key)
        target_bucket_name = self.bucket_name(target.bucket)
        target_key_name = str(target.key)

        if not self.is_dir(path):
            target_bucket = self.s3.Bucket(target_bucket_name)
            object_summery = self.s3.ObjectSummary(source_bucket_name, source_key_name)
            old_source = {'Bucket': object_summery.bucket_name, 'Key': object_summery.key}
            self.boto3_method_with_parameters(
                target_bucket.copy,
                path=target,
                args=(old_source, target_key_name))
            self.boto3_method_with_parameters(object_summery.delete)
            return
        bucket = self.s3.Bucket(source_bucket_name)
        target_bucket = self.s3.Bucket(target_bucket_name)
        for object_summery in bucket.objects.filter(Prefix=source_key_name):
            old_source = {'Bucket': object_summery.bucket_name, 'Key': object_summery.key}
            new_key = object_summery.key.replace(source_key_name, target_key_name)
            self.boto3_method_with_parameters(
                target_bucket.copy,
                path=S3Path(target_bucket_name, new_key),
                args=(old_source, new_key))
            self.boto3_method_with_parameters(object_summery.delete)

    def replace(self, path, target):
        return self.rename(path, target)

    def rmdir(self, path):
        bucket_name = self.bucket_name(path.bucket)
        key_name = str(path.key)
        bucket = self.s3.Bucket(bucket_name)
        for object_summery in bucket.objects.filter(Prefix=key_name):
            self.boto3_method_with_parameters(object_summery.delete, path=path)

    def mkdir(self, path, mode):
        self.boto3_method_with_parameters(
            self.s3.create_bucket,
            path=path,
            kwargs={'Bucket': self.bucket_name(path.bucket)},
        )

    def symlink(self, a, b, target_is_directory=False):
        if not a.exists():
            raise FileNotFoundError(a)
        if b.exists():
            raise FileExistsError(b)
        dest_bucket_name = self.bucket_name(b.bucket)
        dest_key_name = str(b.key)
        dest_object = self.s3.Object(dest_bucket_name, dest_key_name)
        self.boto3_method_with_parameters(
            dest_object.put, kwargs={"Body": b"", "WebsiteRedirectLocation": str(a)}
        )

    def bucket_name(self, path):
        if path is None:
            return
        return str(path.bucket)[1:]

    def boto3_method_with_parameters(self, boto3_method, path=Path('/'), args=(), kwargs=None):
        kwargs = kwargs or {}
        kwargs.update({
            key: value
            for key, value in self.configuration_map[path]
            if key in self._get_action_arguments(boto3_method)
        })
        return boto3_method(*args, **kwargs)

    def generate_prefix(self, path):
        sep = path._flavour.sep
        if not path.key:
            return ''
        key_name = str(path.key)
        if not key_name.endswith(sep):
            return key_name + sep
        return key_name

    def unlink(self, path, *args, **kwargs):
        bucket_name = self.bucket_name(path.bucket)
        key_name = str(path.key)
        bucket = self.s3.Bucket(bucket_name)
        try:
            self.boto3_method_with_parameters(
                bucket.meta.client.delete_object,
                kwargs={"Bucket": bucket_name, "Key": key_name}
            )
        except ClientError:
            raise OSError("/{0}/{1}".format(bucket_name, key_name))

    @lru_cache()
    def _get_action_arguments(self, action):
        if isinstance(action.__doc__, LazyLoadedDocstring):
            docs = action.__doc__._generate()
        else:
            docs = action.__doc__
        return set(
            line.replace(':param ', '').strip().strip(':')
            for line in docs.splitlines()
            if line.startswith(':param ')
        )


def _string_parser(text, *, mode, encoding):
    if isinstance(text, memoryview):
        if 'b' in mode:
            return text
        return text.obj.decode(encoding or 'utf-8')
    if isinstance(text, bytes):
        if 'b' in mode:
            return text
        return text.decode(encoding or 'utf-8')
    if isinstance(text, str):
        if 't' in mode or 'r' == mode:
            return text
        return text.encode(encoding or 'utf-8')
    raise RuntimeError()


class _PathNotSupportedMixin:
    _NOT_SUPPORTED_MESSAGE = '{method} is unsupported on S3 service'

    @classmethod
    def cwd(cls):
        """
        cwd class method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = cls._NOT_SUPPORTED_MESSAGE.format(method=cls.cwd.__qualname__)
        raise NotImplementedError(message)

    @classmethod
    def home(cls):
        """
        home class method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = cls._NOT_SUPPORTED_MESSAGE.format(method=cls.home.__qualname__)
        raise NotImplementedError(message)

    def chmod(self, mode):
        """
        chmod method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.chmod.__qualname__)
        raise NotImplementedError(message)

    def expanduser(self):
        """
        expanduser method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.expanduser.__qualname__)
        raise NotImplementedError(message)

    def lchmod(self, mode):
        """
        lchmod method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.lchmod.__qualname__)
        raise NotImplementedError(message)

    def group(self):
        """
        group method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.group.__qualname__)
        raise NotImplementedError(message)

    def is_block_device(self):
        """
        is_block_device method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.is_block_device.__qualname__)
        raise NotImplementedError(message)

    def is_char_device(self):
        """
        is_char_device method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.is_char_device.__qualname__)
        raise NotImplementedError(message)

    def lstat(self):
        """
        lstat method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.lstat.__qualname__)
        raise NotImplementedError(message)

    def resolve(self):
        """
        resolve method is unsupported on S3 service
        AWS S3 don't have this file system action concept
        """
        message = self._NOT_SUPPORTED_MESSAGE.format(method=self.resolve.__qualname__)
        raise NotImplementedError(message)


_s3_flavour = _S3Flavour()
_s3_accessor = _S3Accessor()


def register_configuration_parameter(path, *, parameters):
    if not isinstance(path, PureS3Path):
        raise TypeError('path argument have to be a {} type. got {}'.format(PureS3Path, type(path)))
    if not isinstance(parameters, dict):
        raise TypeError('parameters argument have to be a dict type. got {}'.format(type(path)))
    _s3_accessor.configuration_map[path].update(**parameters)


class PureS3Path(PurePath):
    """
    PurePath subclass for AWS S3 service.

    S3 is not a file-system but we can look at it like a POSIX system.
    """
    _flavour = _s3_flavour
    __slots__ = ()

    @classmethod
    def from_uri(cls, uri):
        """
        from_uri class method create a class instance from url

        >> from s3path import PureS3Path
        >> PureS3Path.from_url('s3://<bucket>/')
        << PureS3Path('/<bucket>')
        """
        if not uri.startswith('s3://'):
            raise ValueError('...')
        return cls(uri[4:])

    @property
    def bucket(self):
        """
        bucket property
        return a new instance of only the bucket path
        """
        self._absolute_path_validation()
        if not self.is_absolute():
            raise ValueError("relative path don't have bucket")
        try:
            _, bucket, *_ = self.parts
        except ValueError:
            return None
        return type(self)(self._flavour.sep, bucket)

    @property
    def key(self):
        """
        bucket property
        return a new instance of only the key path
        """
        self._absolute_path_validation()
        key = self._flavour.sep.join(self.parts[2:])
        if not key:
            return None
        return type(self)(key)

    def as_uri(self):
        """
        Return the path as a 's3' URI.
        """
        return super().as_uri()

    def _absolute_path_validation(self):
        if not self.is_absolute():
            raise ValueError('relative path have no bucket, key specification')


class S3Path(_PathNotSupportedMixin, Path, PureS3Path):
    """
    Path subclass for AWS S3 service.

    S3Path provide a Python convenient File-System/Path like interface for AWS S3 Service
     using boto3 S3 resource as a driver.

    If boto3 isn't installed in your environment NotImplementedError will be raised.
    """
    __slots__ = ()

    def stat(self):
        """
        Returns information about this path (similarly to boto3's ObjectSummary).
        For compatibility with pathlib, the returned object some similar attributes like os.stat_result.
        The result is looked up at each call to this method
        """
        self._absolute_path_validation()
        if not self.key:
            return None
        return super().stat()

    def exists(self):
        """
        Whether the path points to an existing Bucket, key or key prefix.
        """
        self._absolute_path_validation()
        if not self.bucket:
            return True
        return self._accessor.exists(self)

    def is_dir(self):
        """
        Returns True if the path points to a Bucket or a key prefix, False if it points to a full key path.
        False is also returned if the path doesn’t exist.
        Other errors (such as permission errors) are propagated.
        """
        self._absolute_path_validation()
        if self.bucket and not self.key:
            return True
        return self._accessor.is_dir(self)

    def is_file(self):
        """
        Returns True if the path points to a Bucket key, False if it points to Bucket or a key prefix.
        False is also returned if the path doesn’t exist.
        Other errors (such as permission errors) are propagated.
        """
        self._absolute_path_validation()
        if not self.bucket or not self.key:
            return False
        try:
            return bool(self.stat())
        except ClientError:
            return False

    def iterdir(self):
        """
        When the path points to a Bucket or a key prefix, yield path objects of the directory contents
        """
        self._absolute_path_validation()
        yield from super().iterdir()

    def glob(self, pattern):
        """
        Glob the given relative pattern in the Bucket / key prefix represented by this path,
        yielding all matching files (of any kind)
        """
        # import ipdb; ipdb.set_trace()
        yield from super().glob(pattern)

    def rglob(self, pattern):
        """
        This is like calling S3Path.glob with "**/" added in front of the given relative pattern
        """
        yield from super().rglob(pattern)

    def symlink_to(self, target, target_is_directory=False):
        if not isinstance(target, S3Path):
            target = S3Path(target)
        self._accessor.symlink(target, self, target_is_directory=target.is_dir())

    def is_symlink(self):
        return self._accessor.is_symlink(self)

    def open(self, mode='r', buffering=DEFAULT_BUFFER_SIZE, encoding=None, errors=None, newline=None):
        """
        Opens the Bucket key pointed to by the path, returns a Key file object that you can read/write with
        """
        # non-binary files won't error if given an encoding, but we will open them in
        # binary mode anyway, so we need to fix this first
        if "w" in mode and "b" not in mode and encoding is not None:
            encoding = None
        self._absolute_path_validation()
        if mode not in _SUPPORTED_OPEN_MODES:
            raise ValueError('supported modes are {} got {}'.format(_SUPPORTED_OPEN_MODES, mode))
        if buffering == 0 or buffering == 1:
            raise ValueError('supported buffering values are only block sizes, no 0 or 1')
        if 'b' in mode and encoding:
            raise ValueError("binary mode doesn't take an encoding argument")

        if self._closed:
            self._raise_closed()
        return self._accessor.open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline)

    def owner(self):
        """
        Returns the name of the user owning the Bucket or key.
        Similarly to boto3's ObjectSummary owner attribute
        """
        self._absolute_path_validation()
        if not self.is_file():
            return KeyError('file not found')
        return self._accessor.owner(self)

    def rename(self, target):
        """
        Renames this file or Bucket / key prefix / key to the given target.
        If target exists and is a file, it will be replaced silently if the user has permission.
        If path is a key prefix, it will replace all the keys with the same prefix to the new target prefix.
        Target can be either a string or another S3Path object.
        """
        self._absolute_path_validation()
        if not isinstance(target, type(self)):
            target = type(self)(target)
        target._absolute_path_validation()
        return super().rename(target)

    def replace(self, target):
        """
        Renames this Bucket / key prefix / key to the given target.
        If target points to an existing Bucket / key prefix / key, it will be unconditionally replaced.
        """
        return self.rename(target)

    def unlink(self, missing_ok=False):
        """
        Remove this key from its bucket.
        """
        self._absolute_path_validation()
        # S3 doesn't care if you remove full prefixes or buckets with its delete API
        # so unless we manually check, this call will be dropped through without any
        # validation and could result in data loss
        if self.is_dir():
            raise IsADirectoryError(str(self))
        if not self.is_file():
            raise FileNotFoundError(str(self))
        # XXX: Note: If we don't check if the file exists here, S3 will always return
        # success even if we try to delete a key that doesn't exist. So, if we want
        # to raise a `FileNotFoundError`, we need to manually check if the file exists
        # before we make the API call -- since we want to delete the file anyway,
        # we can just ignore this for now and be satisfied that the file will be removed
        super().unlink()

    def rmdir(self):
        """
        Removes this Bucket / key prefix. The Bucket / key prefix must be empty
        """
        self._absolute_path_validation()
        if self.is_file():
            raise NotADirectoryError()
        if not self.is_dir():
            raise FileNotFoundError()
        return super().rmdir()

    def samefile(self, other_path):
        """
        Returns whether this path points to the same Bucket key as other_path,
        Which can be either a Path object, or a string
        """
        self._absolute_path_validation()
        if not isinstance(other_path, Path):
            other_path = type(self)(other_path)
        return self.bucket == other_path.bucket and self.key == self.key and self.is_file()

    def touch(self, mode=0o666, exist_ok=True):
        """
        Creates a key at this given path.
        If the key already exists,
        the function succeeds if exist_ok is true (and its modification time is updated to the current time),
        otherwise FileExistsError is raised
        """
        if self.exists() and not exist_ok:
            raise FileExistsError()
        self.write_text('')

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        """
        Create a path bucket.
        AWS S3 Service doesn't support folders, therefore the mkdir method will only create the current bucket.
        If the bucket path already exists, FileExistsError is raised.

        If exist_ok is false (the default), FileExistsError is raised if the target Bucket already exists.
        If exist_ok is true, OSError exceptions will be ignored.

        if parents is false (the default), mkdir will create the bucket only if this is a Bucket path.
        if parents is true, mkdir will create the bucket even if the path have a Key path.

        mode argument is ignored.
        """
        try:
            if self.bucket is None:
                raise FileNotFoundError('No bucket in {} {}'.format(type(self), self))
            if self.key is not None and not parents:
                raise FileNotFoundError('Only bucket path can be created, got {}'.format(self))
            if self.bucket.exists():
                raise FileExistsError('Bucket {} already exists'.format(self.bucket))
            return super().mkdir(mode, parents=parents, exist_ok=exist_ok)
        except OSError:
            if not exist_ok:
                raise

    def is_mount(self):
        """
        AWS S3 Service doesn't have mounting feature, There for this method will always return False
        """
        return False

    def is_socket(self):
        """
        AWS S3 Service doesn't have sockets feature, There for this method will always return False
        """
        return False

    def is_fifo(self):
        """
        AWS S3 Service doesn't have fifo feature, There for this method will always return False
        """
        return False

    def _init(self, template=None):
        super()._init(template)
        if template is None:
            self._accessor = _s3_accessor


class S3KeyWritableFileObject(RawIOBase):
    def __init__(
            self, object_summery, *,
            path,
            mode='w',
            buffering=DEFAULT_BUFFER_SIZE,
            encoding=None,
            errors=None,
            newline=None):
        super().__init__()
        self.object_summery = object_summery
        self.path = path
        self.mode = mode
        self.buffering = buffering
        self.encoding = encoding
        self.errors = errors
        self.newline = newline
        self._cache = NamedTemporaryFile(
            mode=self.mode + '+' if 'b' in self.mode else 'b' + self.mode + '+',
            buffering=self.buffering,
            encoding=self.encoding,
            newline=self.newline)
        self._string_parser = partial(_string_parser, mode=self.mode, encoding=self.encoding)

    def __getattr__(self, item):
        try:
            return getattr(self._cache, item)
        except AttributeError:
            return super().__getattribute__(item)

    def writable_check(method):
        @wraps(method)
        def wrapper(self, *args, **kwargs):
            if not self.writable():
                raise UnsupportedOperation('not writable')
            return method(self, *args, **kwargs)
        return wrapper

    def writable(self, *args, **kwargs):
        return 'w' in self.mode

    @writable_check
    def write(self, text):
        self._cache.write(self._string_parser(text))
        self._cache.seek(0)
        _s3_accessor.boto3_method_with_parameters(
            self.object_summery.put,
            path=self.path,
            kwargs={'Body': self._cache}
        )

    def writelines(self, lines):
        self.write(self._string_parser('\n').join(self._string_parser(line) for line in lines))

    def readable(self):
        return False

    def read(self, *args, **kwargs):
        raise UnsupportedOperation('not readable')

    def readlines(self, *args, **kwargs):
        raise UnsupportedOperation('not readable')


class S3KeyReadableFileObject(RawIOBase):
    def __init__(
            self, object_summery, *,
            path,
            mode='b',
            buffering=DEFAULT_BUFFER_SIZE,
            encoding=None,
            errors=None,
            newline=None):
        super().__init__()
        self.object_summery = object_summery
        self.path = path
        self.mode = mode
        self.buffering = buffering
        self.encoding = encoding
        self.errors = errors
        self.newline = newline
        self._streaming_body = None
        self._string_parser = partial(_string_parser, mode=self.mode, encoding=self.encoding)

    def __iter__(self):
        return self

    def __next__(self):
        return self.readline()

    def __getattr__(self, item):
        try:
            return getattr(self._streaming_body, item)
        except AttributeError:
            return super().__getattribute__(item)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def readable_check(method):
        @wraps(method)
        def wrapper(self, *args, **kwargs):
            if not self.readable():
                raise UnsupportedOperation('not readable')
            return method(self, *args, **kwargs)
        return wrapper

    def readable(self):
        if 'r' not in self.mode:
            return False
        with suppress(ClientError):
            if self._streaming_body is None:
                self._streaming_body = _s3_accessor.boto3_method_with_parameters(
                    self.object_summery.get,
                    path=self.path)['Body']
            return True
        return False

    @readable_check
    def read(self, *args, **kwargs):
        return self._string_parser(self._streaming_body.read())

    @readable_check
    def readlines(self, *args, **kwargs):
        return [
            line
            for line in iter(self.readline, self._string_parser(''))
        ]

    @readable_check
    def readline(self):
        with suppress(StopIteration, ValueError):
            line = next(self._streaming_body.iter_lines(chunk_size=self.buffering))
            return self._string_parser(line)
        return self._string_parser(b'')

    def write(self, *args, **kwargs):
        raise UnsupportedOperation('not writable')

    def writelines(self, *args, **kwargs):
        raise UnsupportedOperation('not writable')

    def writable(self, *args, **kwargs):
        return False


class StatResult(namedtuple('BaseStatResult', 'size, last_modified')):
    """
    Base of os.stat_result but with boto3 s3 features
    """

    def __getattr__(self, item):
        if item in vars(stat_result):
            raise UnsupportedOperation('{} do not support {} attribute'.format(type(self).__name__, item))
        return super().__getattribute__(item)

    @property
    def st_size(self):
        return self.size

    @property
    def st_mtime(self):
        return self.last_modified.timestamp()


class S3DirEntry:
    def __init__(self, name, is_dir, size=None, last_modified=None, is_symlink=False):
        self.name = name
        self._is_dir = is_dir
        self._size = size
        self._last_modified = last_modified
        self._stat = StatResult(size=size, last_modified=last_modified)
        self._is_symlink = is_symlink

    def __repr__(self):
        return '{}(name={}, is_dir={}, stat={})'.format(
            type(self).__name__, self.name, self._is_dir, self._stat)

    def inode(self, *args, **kwargs):
        return None

    def is_dir(self):
        return self._is_dir

    def is_file(self):
        return not self._is_dir

    def is_symlink(self, *args, **kwargs):
        return self._is_symlink

    def stat(self):
        return self._stat
