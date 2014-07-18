from rez.build_process import LocalSequentialBuildProcess
from rez.build_system import create_build_system
from rez.resolved_context import ResolvedContext
from rez.release_vcs import create_release_vcs
from rez.vendor import yaml
from rez.exceptions import BuildError, ReleaseError, ReleaseVCSError
import rez.vendor.unittest2 as unittest
from rez.tests.util import TestBase, TempdirMixin, shell_dependent, \
    install_dependent
from rez.resources import clear_caches
import rez.bind.platform
import rez.bind.arch
import rez.bind.os
import rez.bind.python
import shutil
import os.path


class TestRelease(TestBase, TempdirMixin):
    @classmethod
    def setUpClass(cls):
        TempdirMixin.setUpClass()

        path = os.path.dirname(__file__)
        packages_path = os.path.join(path, "data", "release")
        cls.src_root = os.path.join(cls.root, "src")
        cls.install_root = os.path.join(cls.root, "packages")
        shutil.copytree(packages_path, cls.src_root)

        cls.settings = dict(
            packages_path=[cls.install_root],
            release_packages_path=cls.install_root,
            add_bootstrap_path=False,
            resolve_caching=False,
            warn_untimestamped=False,
            implicit_packages=[])

    @classmethod
    def tearDownClass(cls):
        TempdirMixin.tearDownClass()

    @classmethod
    def _create_context(cls, *pkgs):
        # cache clear is needed to clear Resource._listdir cache, which hides
        # newly added packages
        clear_caches()
        return ResolvedContext(pkgs)

    #@shell_dependent
    #@install_dependent
    def test_1(self):
        """Basic release."""
        working_dir = self.src_root
        packagefile = os.path.join(working_dir, "package.yaml")
        with open(packagefile) as f:
            package_data = yaml.load(f.read())

        def _write_package():
            with open(packagefile, 'w') as f:
                f.write(yaml.dump(package_data))
            clear_caches()

        # create the build system
        buildsys = create_build_system(working_dir, verbose=True)
        self.assertEqual(buildsys.name(), "bez")

        # create the vcs
        with self.assertRaises(ReleaseVCSError):
            vcs = create_release_vcs(working_dir)

        stubfile = os.path.join(working_dir, ".stub")
        with open(stubfile, 'w'):
            pass
        vcs = create_release_vcs(working_dir)
        self.assertEqual(vcs.name(), "stub")

        def _create_builder():
            return LocalSequentialBuildProcess(working_dir,
                                               buildsys=buildsys,
                                               vcs=vcs,
                                               ensure_latest=True)

        # do a release
        builder = _create_builder()
        with self.assertRaises(ReleaseError):
            builder.release()

        os.mkdir(self.install_root)
        builder.release()

        # check a file to see the release made it
        filepath = os.path.join(self.install_root, "foo", "1.0", "data", "data.txt")
        self.assertTrue(os.path.exists(filepath))

        # failed release (same version release again)
        clear_caches()
        builder = _create_builder()
        with self.assertRaises(ReleaseError):
            builder.release()

        # update package version and release again
        package_data["version"] = "1.1"
        _write_package()
        builder = _create_builder()
        builder.release()

        # change version to earlier and do failed release attempt

        # release again, this time allow not latest

        # change uuid and do failed release attempt

        # check the vcs contains the tags we expect


def get_test_suites():
    suites = []
    suite = unittest.TestSuite()
    suite.addTest(TestRelease("test_1"))
    suites.append(suite)
    return suites


if __name__ == '__main__':
    unittest.main()