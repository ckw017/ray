import os
from contextlib import contextmanager
from typing import List
from ray.workers.setup_runtime_env import inject_dependencies
import pytest
import sys
import unittest
import tempfile
import yaml
import time

import subprocess

from unittest import mock
import ray
from ray._private.utils import get_conda_env_dir, get_conda_bin_executable
from ray._private.runtime_env import RuntimeEnvDict
from ray.workers.setup_runtime_env import (
    _inject_ray_to_conda_site,
    _resolve_install_from_source_ray_dependencies,
    _current_py_version,
)
from ray.job_config import JobConfig
from ray.test_utils import (run_string_as_driver,
                            run_string_as_driver_nonblocking)

if not os.environ.get("CI"):
    # This flags turns on the local development that link against current ray
    # packages and fall back all the dependencies to current python's site.
    os.environ["RAY_RUNTIME_ENV_LOCAL_DEV_MODE"] = "1"

REQUEST_VERSIONS = ["2.2.0", "2.3.0"]


@pytest.fixture(scope="session")
def conda_envs():
    """Creates two conda env with different requests versions."""
    conda_path = get_conda_bin_executable("conda")
    init_cmd = (f". {os.path.dirname(conda_path)}"
                f"/../etc/profile.d/conda.sh")

    def delete_env(env_name):
        subprocess.run(["conda", "remove", "--name", env_name, "--all", "-y"])

    def create_package_env(env_name, package_version: str):
        delete_env(env_name)
        subprocess.run([
            "conda", "create", "-n", env_name, "-y",
            f"python={_current_py_version()}"
        ])

        _inject_ray_to_conda_site(get_conda_env_dir(env_name))
        ray_deps: List[str] = _resolve_install_from_source_ray_dependencies()
        ray_deps.append(f"requests=={package_version}")
        with tempfile.NamedTemporaryFile("w") as f:
            f.writelines([line + "\n" for line in ray_deps])
            f.flush()

            commands = [
                init_cmd, f"conda activate {env_name}",
                f"python -m pip install -r {f.name}", "conda deactivate"
            ]
            proc = subprocess.run(
                [" && ".join(commands)],
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            if proc.returncode != 0:
                print("pip install failed")
                print(proc.stdout.decode())
                print(proc.stderr.decode())
                assert False

    for package_version in REQUEST_VERSIONS:
        create_package_env(
            env_name=f"package-{package_version}",
            package_version=package_version)

    yield

    for package_version in REQUEST_VERSIONS:
        delete_env(env_name=f"package-{package_version}")


@ray.remote
def get_requests_version():
    import requests  # noqa: E811
    return requests.__version__


@ray.remote
class VersionActor:
    def get_requests_version(self):
        import requests  # noqa: E811
        return requests.__version__


check_remote_client_conda = """
import ray
context = (ray.client("localhost:24001")
              .env({{"conda" : "package-{package_version}"}})
              .connect())
@ray.remote
def get_package_version():
    import requests
    return requests.__version__

assert ray.get(get_package_version.remote()) == "{package_version}"
context.disconnect()
"""


@pytest.mark.skipif(
    os.environ.get("CONDA_DEFAULT_ENV") is None,
    reason="must be run from within a conda environment")
@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
@pytest.mark.parametrize(
    "call_ray_start",
    ["ray start --head --ray-client-server-port 24001 --port 0"],
    indirect=True)
def test_client_tasks_and_actors_inherit_from_driver(conda_envs,
                                                     call_ray_start):
    for i, package_version in enumerate(REQUEST_VERSIONS):
        runtime_env = {"conda": f"package-{package_version}"}
        with ray.client("localhost:24001").env(runtime_env).connect():
            assert ray.get(get_requests_version.remote()) == package_version
            actor_handle = VersionActor.remote()
            assert ray.get(
                actor_handle.get_requests_version.remote()) == package_version

            # Ensure that we can have a second client connect using the other
            # conda environment.
            other_package_version = REQUEST_VERSIONS[(i + 1) % 2]
            run_string_as_driver(
                check_remote_client_conda.format(
                    package_version=other_package_version))


@pytest.mark.skipif(
    os.environ.get("CONDA_DEFAULT_ENV") is None,
    reason="must be run from within a conda environment")
@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_task_actor_conda_env(conda_envs, shutdown_only):
    ray.init()

    # Basic conda runtime env
    for package_version in REQUEST_VERSIONS:
        runtime_env = {"conda": f"package-{package_version}"}

        task = get_requests_version.options(runtime_env=runtime_env)
        assert ray.get(task.remote()) == package_version

        actor = VersionActor.options(runtime_env=runtime_env).remote()
        assert ray.get(actor.get_requests_version.remote()) == package_version

    # Runtime env should inherit to nested task
    @ray.remote
    def wrapped_version():
        return ray.get(get_requests_version.remote())

    @ray.remote
    class Wrapper:
        def wrapped_version(self):
            return ray.get(get_requests_version.remote())

    for package_version in REQUEST_VERSIONS:
        runtime_env = {"conda": f"package-{package_version}"}

        task = wrapped_version.options(runtime_env=runtime_env)
        assert ray.get(task.remote()) == package_version

        actor = Wrapper.options(runtime_env=runtime_env).remote()
        assert ray.get(actor.wrapped_version.remote()) == package_version


@pytest.mark.skipif(
    os.environ.get("CONDA_DEFAULT_ENV") is None,
    reason="must be run from within a conda environment")
@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_job_config_conda_env(conda_envs, shutdown_only):
    for package_version in REQUEST_VERSIONS:
        runtime_env = {"conda": f"package-{package_version}"}
        ray.init(job_config=JobConfig(runtime_env=runtime_env))
        assert ray.get(get_requests_version.remote()) == package_version
        ray.shutdown()


def test_get_conda_env_dir(tmp_path):
    from pathlib import Path
    """
    Typical output of `conda env list`, for context:

    base                 /Users/scaly/anaconda3
    my_env_1             /Users/scaly/anaconda3/envs/my_env_1

    For this test, `tmp_path` is a stand-in for `Users/scaly/anaconda3`.
    """

    # Simulate starting in an env named tf1.
    d = tmp_path / "envs" / "tf1"
    Path.mkdir(d, parents=True)
    with mock.patch.dict(os.environ, {
            "CONDA_PREFIX": str(d),
            "CONDA_DEFAULT_ENV": "tf1"
    }):
        with pytest.raises(ValueError):
            # Env tf2 should not exist.
            env_dir = get_conda_env_dir("tf2")
        tf2_dir = tmp_path / "envs" / "tf2"
        Path.mkdir(tf2_dir, parents=True)
        env_dir = get_conda_env_dir("tf2")
        assert (env_dir == str(tmp_path / "envs" / "tf2"))

    # Simulate starting in (base) conda env.
    with mock.patch.dict(os.environ, {
            "CONDA_PREFIX": str(tmp_path),
            "CONDA_DEFAULT_ENV": "base"
    }):
        with pytest.raises(ValueError):
            # Env tf3 should not exist.
            env_dir = get_conda_env_dir("tf3")
        # Env tf2 still should exist.
        env_dir = get_conda_env_dir("tf2")
        assert (env_dir == str(tmp_path / "envs" / "tf2"))


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_conda_create_task(shutdown_only):
    """Tests dynamic creation of a conda env in a task's runtime env."""
    ray.init()
    runtime_env = {
        "conda": {
            "dependencies": ["pip", {
                "pip": ["pip-install-test==0.5"]
            }]
        }
    }

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with pytest.raises(ModuleNotFoundError):
        # Ensure pip-install-test is not installed on the test machine
        import pip_install_test  # noqa
    with pytest.raises(ray.exceptions.RayTaskError) as excinfo:
        ray.get(f.remote())
    assert "ModuleNotFoundError" in str(excinfo.value)
    assert ray.get(f.options(runtime_env=runtime_env).remote())


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_conda_create_job_config(shutdown_only):
    """Tests dynamic conda env creation in a runtime env in the JobConfig."""

    runtime_env = {
        "conda": {
            "dependencies": ["pip", {
                "pip": ["pip-install-test==0.5"]
            }]
        }
    }
    ray.init(job_config=JobConfig(runtime_env=runtime_env))

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with pytest.raises(ModuleNotFoundError):
        # Ensure pip-install-test is not installed on the test machine
        import pip_install_test  # noqa
    assert ray.get(f.remote())


def test_inject_dependencies():
    num_tests = 4
    conda_dicts = [None] * num_tests
    outputs = [None] * num_tests

    conda_dicts[0] = {}
    outputs[0] = {
        "dependencies": ["python=7.8", "pip", {
            "pip": ["ray==1.2.3"]
        }]
    }

    conda_dicts[1] = {"dependencies": ["blah"]}
    outputs[1] = {
        "dependencies": ["blah", "python=7.8", "pip", {
            "pip": ["ray==1.2.3"]
        }]
    }

    conda_dicts[2] = {"dependencies": ["blah", "pip"]}
    outputs[2] = {
        "dependencies": ["blah", "pip", "python=7.8", {
            "pip": ["ray==1.2.3"]
        }]
    }

    conda_dicts[3] = {"dependencies": ["blah", "pip", {"pip": ["some_pkg"]}]}
    outputs[3] = {
        "dependencies": [
            "blah", "pip", {
                "pip": ["ray==1.2.3", "some_pkg"]
            }, "python=7.8"
        ]
    }

    for i in range(num_tests):
        output = inject_dependencies(conda_dicts[i], "7.8", ["ray==1.2.3"])
        error_msg = (f"failed on input {i}."
                     f"Output: {output} \n"
                     f"Expected output: {outputs[i]}")
        assert (output == outputs[i]), error_msg


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
@pytest.mark.parametrize(
    "call_ray_start",
    ["ray start --head --ray-client-server-port 24001 --port 0"],
    indirect=True)
def test_conda_create_ray_client(call_ray_start):
    """Tests dynamic conda env creation in RayClient."""

    runtime_env = {
        "conda": {
            "dependencies": ["pip", {
                "pip": ["pip-install-test==0.5"]
            }]
        }
    }

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with ray.client("localhost:24001").env(runtime_env).connect():
        with pytest.raises(ModuleNotFoundError):
            # Ensure pip-install-test is not installed on the test machine
            import pip_install_test  # noqa
        assert ray.get(f.remote())

    with ray.client("localhost:24001").connect():
        with pytest.raises(ModuleNotFoundError):
            # Ensure pip-install-test is not installed in a client that doesn't
            # use the runtime_env
            ray.get(f.remote())


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
@pytest.mark.parametrize("pip_as_str", [True, False])
def test_pip_task(shutdown_only, pip_as_str, tmp_path):
    """Tests pip installs in the runtime env specified in f.options()."""

    ray.init()
    if pip_as_str:
        d = tmp_path / "pip_requirements"
        d.mkdir()
        p = d / "requirements.txt"
        requirements_txt = """
        pip-install-test==0.5
        """
        p.write_text(requirements_txt)
        runtime_env = {"pip": str(p)}
    else:
        runtime_env = {"pip": ["pip-install-test==0.5"]}

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with pytest.raises(ModuleNotFoundError):
        # Ensure pip-install-test is not installed on the test machine
        import pip_install_test  # noqa
    with pytest.raises(ray.exceptions.RayTaskError) as excinfo:
        ray.get(f.remote())
    assert "ModuleNotFoundError" in str(excinfo.value)
    assert ray.get(f.options(runtime_env=runtime_env).remote())


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_pip_ray_serve(shutdown_only):
    """Tests that ray[serve] can be included as a pip dependency."""
    ray.init()
    runtime_env = {"pip": ["pip-install-test==0.5", "ray[serve]"]}

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with pytest.raises(ModuleNotFoundError):
        # Ensure pip-install-test is not installed on the test machine
        import pip_install_test  # noqa
    with pytest.raises(ray.exceptions.RayTaskError) as excinfo:
        ray.get(f.remote())
    assert "ModuleNotFoundError" in str(excinfo.value)
    assert ray.get(f.options(runtime_env=runtime_env).remote())


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
@pytest.mark.parametrize("pip_as_str", [True, False])
def test_pip_job_config(shutdown_only, pip_as_str, tmp_path):
    """Tests dynamic installation of pip packages in a task's runtime env."""

    if pip_as_str:
        d = tmp_path / "pip_requirements"
        d.mkdir()
        p = d / "requirements.txt"
        requirements_txt = """
        pip-install-test==0.5
        """
        p.write_text(requirements_txt)
        runtime_env = {"pip": str(p)}
    else:
        runtime_env = {"pip": ["pip-install-test==0.5"]}

    ray.init(job_config=JobConfig(runtime_env=runtime_env))

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with pytest.raises(ModuleNotFoundError):
        # Ensure pip-install-test is not installed on the test machine
        import pip_install_test  # noqa
    assert ray.get(f.remote())


@pytest.mark.skipif(sys.platform == "win32", reason="Unsupported on Windows.")
@pytest.mark.parametrize("use_working_dir", [True, False])
def test_conda_input_filepath(use_working_dir, tmp_path):
    conda_dict = {"dependencies": ["pip", {"pip": ["pip-install-test==0.5"]}]}
    d = tmp_path / "pip_requirements"
    d.mkdir()
    p = d / "environment.yml"

    p.write_text(yaml.dump(conda_dict))

    if use_working_dir:
        runtime_env_dict = RuntimeEnvDict({
            "working_dir": str(d),
            "conda": "environment.yml"
        })
    else:
        runtime_env_dict = RuntimeEnvDict({"conda": str(p)})

    output_conda_dict = runtime_env_dict.get_parsed_dict().get("conda")
    assert output_conda_dict == conda_dict


@unittest.skipIf(sys.platform == "win32", "Fail to create temp dir.")
def test_experimental_package(shutdown_only):
    ray.init(num_cpus=2)
    pkg = ray.experimental.load_package(
        os.path.join(
            os.path.dirname(__file__),
            "../experimental/packaging/example_pkg/ray_pkg.yaml"))
    a = pkg.MyActor.remote()
    assert ray.get(a.f.remote()) == "hello world"
    assert ray.get(pkg.my_func.remote()) == "hello world"


@unittest.skipIf(sys.platform == "win32", "Fail to create temp dir.")
def test_experimental_package_lazy(shutdown_only):
    pkg = ray.experimental.load_package(
        os.path.join(
            os.path.dirname(__file__),
            "../experimental/packaging/example_pkg/ray_pkg.yaml"))
    ray.init(num_cpus=2)
    a = pkg.MyActor.remote()
    assert ray.get(a.f.remote()) == "hello world"
    assert ray.get(pkg.my_func.remote()) == "hello world"


@unittest.skipIf(sys.platform == "win32", "Fail to create temp dir.")
def test_experimental_package_github(shutdown_only):
    ray.init(num_cpus=2)
    pkg = ray.experimental.load_package(
        "http://raw.githubusercontent.com/ray-project/ray/master/"
        "python/ray/experimental/packaging/example_pkg/ray_pkg.yaml")
    a = pkg.MyActor.remote()
    assert ray.get(a.f.remote()) == "hello world"
    assert ray.get(pkg.my_func.remote()) == "hello world"


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
@pytest.mark.parametrize(
    "call_ray_start",
    ["ray start --head --ray-client-server-port 24001 --port 0"],
    indirect=True)
def test_client_working_dir_filepath(call_ray_start, tmp_path):
    """Test that pip and conda relative filepaths work with working_dir."""

    working_dir = tmp_path / "requirements"
    working_dir.mkdir()

    pip_file = working_dir / "requirements.txt"
    requirements_txt = """
    pip-install-test==0.5
    """
    pip_file.write_text(requirements_txt)
    runtime_env_pip = {
        "working_dir": str(working_dir),
        "pip": "requirements.txt"
    }

    conda_file = working_dir / "environment.yml"
    conda_dict = {"dependencies": ["pip", {"pip": ["pip-install-test==0.5"]}]}
    conda_str = yaml.dump(conda_dict)
    conda_file.write_text(conda_str)
    runtime_env_conda = {
        "working_dir": str(working_dir),
        "conda": "environment.yml"
    }

    @ray.remote
    def f():
        import pip_install_test  # noqa
        return True

    with ray.client("localhost:24001").connect():
        with pytest.raises(ModuleNotFoundError):
            # Ensure pip-install-test is not installed in a client that doesn't
            # use the runtime_env
            ray.get(f.remote())

    for runtime_env in [runtime_env_pip, runtime_env_conda]:
        with ray.client("localhost:24001").env(runtime_env).connect():
            with pytest.raises(ModuleNotFoundError):
                # Ensure pip-install-test is not installed on the test machine
                import pip_install_test  # noqa
            assert ray.get(f.remote())


install_env_script = """
import ray
import time
job_config = ray.job_config.JobConfig(runtime_env={env})
ray.init(address="auto", job_config=job_config)
@ray.remote
def f():
    return "hello"
f.remote()
# Give the env 5 seconds to begin installing in a new worker.
time.sleep(5)
"""


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_env_installation_nonblocking(shutdown_only):
    """Test fix for https://github.com/ray-project/ray/issues/16226."""
    env1 = {"pip": ["pip-install-test==0.5"]}
    job_config = ray.job_config.JobConfig(runtime_env=env1)

    ray.init(job_config=job_config)

    @ray.remote
    def f():
        return "hello"

    # Warm up a worker because it takes time to start.
    ray.get(f.remote())

    def assert_tasks_finish_quickly(total_sleep_s=0.1):
        """Call f every 0.01 seconds for total time total_sleep_s."""
        gap_s = 0.01
        for i in range(int(total_sleep_s / gap_s)):
            start = time.time()
            ray.get(f.remote())
            # Env installation takes around 10 to 60 seconds.  If we fail the
            # below assert, we can be pretty sure an env installation blocked
            # the task.
            assert time.time() - start < 1.0
            time.sleep(gap_s)

    assert_tasks_finish_quickly()

    env2 = {"pip": ["pip-install-test==0.5", "requests"]}
    f.options(runtime_env=env2).remote()
    # Check that installing env2 above does not block tasks using env1.
    assert_tasks_finish_quickly()

    proc = run_string_as_driver_nonblocking(
        install_env_script.format(env=env1))
    # Check that installing env1 in a new worker in the script above does not
    # block other tasks that use env1.
    assert_tasks_finish_quickly(total_sleep_s=5)
    proc.kill()
    proc.wait()


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_simultaneous_install(shutdown_only):
    """Test that two envs can be installed without affecting each other."""
    ray.init()

    @ray.remote
    class VersionWorker:
        def __init__(self, key):
            self.key = key

        def get(self):
            import requests
            return (self.key, requests.__version__)

    # Before we used a global lock on conda installs, these two envs would be
    # installed concurrently, leading to errors:
    # https://github.com/ray-project/ray/issues/17086
    # Now we use a global lock, so the envs are installed sequentially.
    worker_1 = VersionWorker.options(runtime_env={
        "pip": ["requests==2.2.0"]
    }).remote(key=1)
    worker_2 = VersionWorker.options(runtime_env={
        "pip": ["requests==2.3.0"]
    }).remote(key=2)

    assert ray.get(worker_1.get.remote()) == (1, "2.2.0")
    assert ray.get(worker_2.get.remote()) == (2, "2.3.0")


@contextmanager
def chdir(dir):
    old_dir = os.getcwd()
    os.chdir(dir)
    yield
    os.chdir(old_dir)


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_runtime_env_override(call_ray_start):
    # https://github.com/ray-project/ray/issues/16481

    with tempfile.TemporaryDirectory() as tmpdir, chdir(tmpdir):
        ray.init(address="auto", namespace="test")

        @ray.remote
        class Child:
            def getcwd(self):
                import os
                return os.getcwd()

            def read(self, path):
                return open(path).read()

            def ready(self):
                pass

        @ray.remote
        class Parent:
            def spawn_child(self, name, runtime_env):
                child = Child.options(
                    lifetime="detached", name=name,
                    runtime_env=runtime_env).remote()
                ray.get(child.ready.remote())

        Parent.options(lifetime="detached", name="parent").remote()
        ray.shutdown()

        with open("hello", "w") as f:
            f.write("world")

        job_config = ray.job_config.JobConfig(runtime_env={"working_dir": "."})
        ray.init(address="auto", namespace="test", job_config=job_config)

        os.remove("hello")

        parent = ray.get_actor("parent")

        env = ray.get_runtime_context().runtime_env
        del env["working_dir"]  # make sure to directly use the direcotry
        print("Spawning with env:", env)
        ray.get(parent.spawn_child.remote("child", env))

        child = ray.get_actor("child")
        child_cwd = ray.get(child.getcwd.remote())
        # Child should be in tmp runtime resource dir.
        assert child_cwd != os.getcwd(), (child_cwd, os.getcwd())
        assert ray.get(child.read.remote("hello")) == "world"

        ray.shutdown()


@pytest.mark.skipif(
    os.environ.get("CI") and sys.platform != "linux",
    reason="This test is only run on linux CI machines.")
def test_runtime_env_inheritance_regression(shutdown_only):
    # https://github.com/ray-project/ray/issues/16479
    with tempfile.TemporaryDirectory() as tmpdir, chdir(tmpdir):
        with open("hello", "w") as f:
            f.write("world")

        job_config = ray.job_config.JobConfig(runtime_env={"working_dir": "."})
        ray.init(job_config=job_config)

        with open("hello", "w") as f:
            f.write("file should already been cached")

        @ray.remote
        class Test:
            def f(self):
                return open("hello").read()

        env1 = ray.get_runtime_context().runtime_env
        del env1["working_dir"]
        print("Using env:", env1)
        t = Test.options(runtime_env=env1).remote()
        assert ray.get(t.f.remote()) == "world"

        # Using working_dir is not supported
        env2 = ray.get_runtime_context().runtime_env
        assert "working_dir" in env2
        with pytest.raises(NotImplementedError):
            t = Test.options(runtime_env=env2).remote()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-sv", __file__]))
