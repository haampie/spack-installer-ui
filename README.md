Experiment with new Spack install interface for parallel builds.

Note: this is just a dummy user interface, nothing gets built or installed.

Features:

* basic GNU jobserver / client
* implicit installs disappear from the screen after the build succeeds
* explicit installs remain (in bold white)
* you can press <kbd>v</kbd> to toggle between overview and logs
* minor: you can press <kbd>1</kbd>, <kbd>2</kbd>, ..., to get logs of a specific package if multiple build in parallel

To try it out:

```console
$ git clone https://github.com/haampie/spack-installer-ui.git
$ ./spack-installer-ui/spack-installer.py
```
