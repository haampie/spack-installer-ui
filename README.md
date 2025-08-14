Experiment with new Spack install interface for parallel builds.

**Trying it out**

1. ```console
   $ git clone https://github.com/haampie/spack-installer-ui.git
   $ cd spack-installer-ui
   $ ./spack-install.py
   $ ./spack-install.py -j4
   ```
2. During the install demo you can change the output mode:
   * Press <kbd>v</kbd> to toggle between overview and logs
   * More specifically, press <kbd>1</kbd>, <kbd>2</kbd>, ..., to follow logs of a particular package (if multiple are being installed in parallel)

**Note**: this is just a dummy user interface, nothing gets actually built or installed, it won't pollute your system or Spack instance.

**Features**

* basic GNU jobserver / client
* implicit installs disappear from the screen after the build succeeds
* explicit installs remain and are displayed in bold white
* toggling between output modes (overview / logs)
