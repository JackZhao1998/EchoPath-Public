/*
 * EchoPath.app launcher.
 *
 * macOS grants Accessibility and Screen Recording to an application, not to a
 * script. A shell wrapper that execs Python would hand those grants to the
 * interpreter ("python 3.12"), covering every program it ever runs. Instead
 * this binary links libpython and calls Py_BytesMain, so the process image
 * stays Contents/MacOS/EchoPath and the grant belongs to EchoPath alone.
 *
 * Workers the Control Room starts re-run this binary with "-m echopath.cli",
 * so their desktop actions are attributed to EchoPath too.
 *
 * Paths are baked in at build time by packaging/macos/make_app.py.
 */

#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

extern int Py_BytesMain(int argc, char **argv);

#ifndef ECHOPATH_PROJECT
#define ECHOPATH_PROJECT "."
#endif
#ifndef ECHOPATH_PYTHONHOME
#define ECHOPATH_PYTHONHOME ""
#endif
#ifndef ECHOPATH_SITE
#define ECHOPATH_SITE ""
#endif

#define MAX_ARGS 256

/* An interpreter-style first argument (worker processes, multiprocessing
 * helpers) runs Python as-is instead of starting the app again. */
static int is_interpreter_call(const char *arg) {
    static const char *flags[] = {"-c", "-m", "-E", "-S", "-I", "-u", "-X", "-B", "-O", "-"};
    for (size_t i = 0; i < sizeof(flags) / sizeof(flags[0]); i++) {
        if (strcmp(arg, flags[i]) == 0) {
            return 1;
        }
    }
    return arg[0] != '-';        /* a script path */
}

static void redirect_log(void) {
    const char *home = getenv("HOME");
    if (home == NULL) {
        return;
    }
    char dir[PATH_MAX];
    char path[PATH_MAX];
    snprintf(dir, sizeof(dir), "%s/.echopath", home);
    mkdir(dir, 0755);
    snprintf(path, sizeof(path), "%s/app.log", dir);
    if (freopen(path, "a", stdout) == NULL) {
        return;
    }
    freopen(path, "a", stderr);
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);
    time_t now = time(NULL);
    fprintf(stderr, "--- launching EchoPath at %s", ctime(&now));
}

int main(int argc, char **argv) {
    char exec_path[PATH_MAX];
    uint32_t size = (uint32_t)sizeof(exec_path);
    if (_NSGetExecutablePath(exec_path, &size) != 0) {
        snprintf(exec_path, sizeof(exec_path), "%s", argv[0]);
    }

    /* Drop the process serial number LaunchServices sometimes appends. */
    char *args[MAX_ARGS];
    int count = 0;
    for (int i = 1; i < argc && count < MAX_ARGS - 6; i++) {
        if (strncmp(argv[i], "-psn_", 5) != 0) {
            args[count++] = argv[i];
        }
    }
    int interpreter = count > 0 && is_interpreter_call(args[0]);

    /* Workers keep the output files their parent gave them. */
    if (!interpreter) {
        redirect_log();
    }

    char pythonpath[PATH_MAX * 2];
    snprintf(pythonpath, sizeof(pythonpath), "%s/src:%s", ECHOPATH_PROJECT, ECHOPATH_SITE);
    /* PYTHONHOME finds the standard library; PYTHONPATH adds the package and
     * the virtualenv's site-packages, since PYTHONHOME disables venv detection. */
    setenv("PYTHONHOME", ECHOPATH_PYTHONHOME, 1);
    setenv("PYTHONPATH", pythonpath, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("ECHOPATH_BUNDLED", "1", 1);

    if (!interpreter && chdir(ECHOPATH_PROJECT) != 0) {
        fprintf(stderr, "echopath: cannot enter %s\n", ECHOPATH_PROJECT);
        return 1;
    }

    char *py_argv[MAX_ARGS];
    int n = 0;
    py_argv[n++] = exec_path;
    if (!interpreter) {
        py_argv[n++] = "-u";
        py_argv[n++] = "-m";
        py_argv[n++] = "echopath.app";
    }
    for (int i = 0; i < count; i++) {
        py_argv[n++] = args[i];
    }
    py_argv[n] = NULL;
    return Py_BytesMain(n, py_argv);
}
