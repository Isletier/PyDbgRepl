const std = @import("std");
const cstd = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("stdlib.h");
    @cInclude("fcntl.h");
});


pub fn openPty() !struct { master: std.posix.fd_t, slave: std.posix.fd_t } {
    const master = cstd.posix_openpt(cstd.O_RDWR | cstd.O_NOCTTY);
    if (master < 0) return error.OpenPtFailed;
    errdefer _ = std.os.linux.close(master);

    if (cstd.grantpt(master) != 0)
        return error.GrantPtFailed;
    if (cstd.unlockpt(master) != 0)
        return error.UnlockPtFailed;

    var buf: [64]u8 = undefined;
    if (cstd.ptsname_r(master, &buf, buf.len) != 0)
        return error.PtsnameFailed;

    const slave = try std.posix.openat(std.posix.AT.FDCWD, std.mem.sliceTo(&buf, 0), .{ .ACCMODE = .RDWR, .NOCTTY = true }, 0);
    return .{ .master = master, .slave = slave };
}

const vm_t = enum {
    python,
    jython
};

const log_level_t = enum(u32) {
    critical    = 0,
    info        = 1,
    debug       = 2,
    verbose     = 3
};

const qt_support_t = enum {
    auto,
    pyqt5,
    pyqt4,
    pyside,
    pyside2,
    none
};

const args_options = struct {
    port:           u64             = 0,
    ppid:           u64             = 0,
    vm_type:        ?vm_t           = null,
    preimport:      ?[]const u8     = null,
    log_file:       ?[]const u8     = null,
    log_level:      log_level_t     = .critical,
    qt_support:     qt_support_t    = .auto,
    startup_msg:    bool            = false,
    module:         bool            = false,
    file:           ?[]const u8     = null
};

const obligatory_run_arguments = [_][]const u8 {
    "--server",
    "--json-dap-http",
    "--cmd-line",
    "--skip-notify-stdin",
};

const sanitize_run_arguments = [_][]const u8 {
    "--client",
    "--access-token",
    "--debug-mode",
    "--multiproc",
    "--multiprocess",
    "--save-signatures",
    "--save-threading",
    "--save-asyncio",
    "--json-dap",
    "--protocol-quoted-line",
    "--protocol-http",
    "--DEBUG"
};

fn sanitize_forbidden(arg: []const u8) bool {
    for (sanitize_run_arguments) |s_arg| {
        if (std.mem.eql(u8, s_arg, arg)) {
            return true;
        }
    }

    return false;
}

fn sanitize_obligatory(arg: []const u8) bool {
    for (obligatory_run_arguments) |o_arg| {
        if (std.mem.eql(u8, o_arg, arg)) {
             return true;
        }
    }

    return false;
}

const launch_errors = error {
    forbidden_flag,
    obligatory_flag,
    missing_argument,
    invalid_argument_parameter
};

fn vm_type_reflection(str: []const u8) !vm_t {
    if (std.mem.eql(u8, str, "jython")) {
        return vm_t.jython;
    } else
    if (std.mem.eql(u8, str, "python")) {
        return vm_t.python;
    } else {
        return launch_errors.invalid_argument_parameter;
    }
}

fn log_level_reflection(str: []const u8) !log_level_t {
    if (std.mem.eql(u8, str, "critical")) {
        return log_level_t.critical;
    } else 
    if (std.mem.eql(u8, str, "info")) {
        return log_level_t.info;
    } else 
    if (std.mem.eql(u8, str, "debug")) {
        return log_level_t.debug;
    } else 
    if (std.mem.eql(u8, str, "verbose")) {
        return log_level_t.verbose;
    } else {
        return launch_errors.invalid_argument_parameter;
    }
}

fn qt_support_reflection(str: []const u8) !qt_support_t {
    if (std.mem.eql(u8, str, "auto"))    return .auto;
    if (std.mem.eql(u8, str, "pyqt5"))   return .pyqt5;
    if (std.mem.eql(u8, str, "pyqt4"))   return .pyqt4;
    if (std.mem.eql(u8, str, "pyside"))  return .pyside;
    if (std.mem.eql(u8, str, "pyside2")) return .pyside2;
    if (std.mem.eql(u8, str, "none"))    return .none;
    return launch_errors.invalid_argument_parameter;
}

fn serialize_launch_args(out: *std.Io.Writer, args: []const [*:0]const u8) ![]const [*:0]const u8 {
    const flag = std.mem.span(args[0]);

    if (std.mem.eql(u8, flag, "--file")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }

        RUN_CTX.args_opt.file = std.mem.span(args[1]);
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--port")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }

        RUN_CTX.args_opt.port = try std.fmt.parseInt(u64, std.mem.span(args[1]), 10);
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--ppid")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }

        RUN_CTX.args_opt.ppid = try std.fmt.parseInt(u64, std.mem.span(args[1]), 10);
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--vm_type")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }

        if (vm_type_reflection(std.mem.span(args[1]))) |value| {
            RUN_CTX.args_opt.vm_type = value;
        } else |_| {
            try out.print("error: invalid vm_type value '{s}'\n", .{std.mem.span(args[1])});
            return launch_errors.invalid_argument_parameter;
        }
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--preimport")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }

        RUN_CTX.args_opt.preimport = std.mem.span(args[1]);
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--log_file")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }
        RUN_CTX.args_opt.log_file = std.mem.span(args[1]);
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--log_level")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }
        if (log_level_reflection(std.mem.span(args[1]))) |value| {
            RUN_CTX.args_opt.log_level = value;
        } else |_| {
            try out.print("error: invalid log level value '{s}'\n", .{std.mem.span(args[1])});
            return launch_errors.invalid_argument_parameter;
        }
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--qt_support")) {
        if (args.len < 2) {
            try out.print("error: expected parameter value for {s}\n", .{flag});
            return launch_errors.missing_argument;
        }
        if (qt_support_reflection(std.mem.span(args[1]))) |value| {
            RUN_CTX.args_opt.qt_support = value;
        } else |_| {
            try out.print("error: invalid qt_support value '{s}'\n", .{std.mem.span(args[1])});
            return launch_errors.invalid_argument_parameter;
        }
        return args[2..];
    } else if (std.mem.eql(u8, flag, "--print-in-debugger-startup")) {
        RUN_CTX.args_opt.startup_msg = true;
        return args[1..];
    } else if (std.mem.eql(u8, flag, "--module")) {
        RUN_CTX.args_opt.module = true;
        return args[1..];
    } else {
        return args[1..];
    }
}

fn process_args(al: std.mem.Allocator, out: *std.Io.Writer, args_: std.process.Args) !void {
    var args = args_.vector;

    while (args.len != 0) {
        const arg_s: []const u8 = std.mem.span(args[0]);
        if(sanitize_forbidden(arg_s)) {
            try out.print("error: pydevd original flag {s} is not supported", .{arg_s});
            return launch_errors.forbidden_flag;
        }

        if(sanitize_obligatory(arg_s)) {
            try out.print("error: pydevd original flag {s} is enabled by default", .{arg_s});
            return launch_errors.obligatory_flag;
        }

        args = try serialize_launch_args(out, args);

        if(std.mem.eql(u8, "--file", arg_s)) {
            const tail_args = try al.alloc([]const u8, args.len);
            var i: usize = 0;
            while(i < args.len) {
                tail_args[i] = std.mem.span(args[i]);
                i += 1;
            }
            RUN_CTX.args = tail_args;
            return;
        }
    }

    return;
}

const env_options = struct {
    PYDEVD_USE_SYS_MONITORING:                      ?bool       = null,
    PYDEVD_USE_CYTHON:                              ?bool       = null,
    PYDEVD_USE_FRAME_EVAL:                          ?bool       = null,
    PYDEVD_DEBUG_FILE:                              ?[]const u8 = null,
    PYDEVD_LOG_TIME:                                bool        = true,
    GEVENT_SUPPORT:                                 bool        = false,
    GEVENT_SHOW_PAUSED_GREENLETS:                   bool        = false,
    GEVENT_SUPPORT_NOT_SET_MSG:                     ?[]const u8 = null,
    PYDEVD_LOAD_NATIVE_LIB:                         ?[]const u8 = null,
    PYDEVD_DISABLE_FILE_VALIDATION:                 bool        = false,
    PYDEVD_LOAD_VALUES_ASYNC:                       bool        = false,
    PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS:   f64         = 0.5,
    PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE:        bool        = false,
    PYDEVD_WARN_EVALUATION_TIMEOUT:                 f64         = 3.0,
    PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT:  bool        = false,
    PYDEVD_UNBLOCK_THREADS_TIMEOUT:                 f64         = -1.0,
    PYDEVD_INTERRUPT_THREAD_TIMEOUT:                f64         = -1.0,
    PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS:        i64         = 100,
    PYDEVD_CONTAINER_BUCKET_SIZE:                   i64         = 1000,
    PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS:       i64         = 500,
    PYDEVD_CONTAINER_NUMPY_MAX_ITEMS:               i64         = 500,
    PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT:               f64         = 0.5,
    PYDEVD_PANDAS_MAX_ROWS:                         i64         = 60,
    PYDEVD_PANDAS_MAX_COLS:                         i64         = 10,
    PYDEVD_PANDAS_MAX_COLWIDTH:                     i64         = 50,
};

const env_sanitize = [_][]const u8 {
    "PYDEVD_DEBUG",
    "PYDEV_DEBUG",
    "PYCHARM_DEBUG",
    "PYDEVD_DEBUG_FILE",
    "PYDEVD_IPYTHON_COMPATIBLE_DEBUGGING",
    "PYDEVD_IPYTHON_CONTEXT"
};

const connect_options = struct{};

const run_context = struct {
    env:            env_options             = .{},
    args_opt:       args_options            = .{},
    args:           []const []const u8      = &.{},
    connect_opt:    connect_options         = .{}
};

var RUN_CTX: run_context = .{};

fn parse_bool(str: []const u8) !bool {
    if (std.mem.eql(u8, str, "1") or std.mem.eql(u8, str, "true"))  return true;
    if (std.mem.eql(u8, str, "0") or std.mem.eql(u8, str, "false")) return false;
    return launch_errors.invalid_argument_parameter;
}

fn process_envs(out: *std.Io.Writer, env_map: *const std.process.Environ.Map) !void {
    if (env_map.get("PYDEVD_USE_SYS_MONITORING")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_USE_SYS_MONITORING = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_USE_SYS_MONITORING\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_USE_CYTHON")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_USE_CYTHON = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_USE_CYTHON\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_USE_FRAME_EVAL")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_USE_FRAME_EVAL = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_USE_FRAME_EVAL\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_DEBUG_FILE")) |val| {
        RUN_CTX.env.PYDEVD_DEBUG_FILE = val;
    }
    if (env_map.get("PYDEVD_LOG_TIME")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_LOG_TIME = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_LOG_TIME\n", .{val});
        }
    }
    if (env_map.get("GEVENT_SUPPORT")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.GEVENT_SUPPORT = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for GEVENT_SUPPORT\n", .{val});
        }
    }
    if (env_map.get("GEVENT_SHOW_PAUSED_GREENLETS")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.GEVENT_SHOW_PAUSED_GREENLETS = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for GEVENT_SHOW_PAUSED_GREENLETS\n", .{val});
        }
    }
    if (env_map.get("GEVENT_SUPPORT_NOT_SET_MSG")) |val| {
        RUN_CTX.env.GEVENT_SUPPORT_NOT_SET_MSG = val;
    }
    if (env_map.get("PYDEVD_LOAD_NATIVE_LIB")) |val| {
        RUN_CTX.env.PYDEVD_LOAD_NATIVE_LIB = val;
    }
    if (env_map.get("PYDEVD_DISABLE_FILE_VALIDATION")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_DISABLE_FILE_VALIDATION = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_DISABLE_FILE_VALIDATION\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_LOAD_VALUES_ASYNC")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_LOAD_VALUES_ASYNC = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_LOAD_VALUES_ASYNC\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS")) |val| {
        if (std.fmt.parseFloat(f64, val)) |f| {
            RUN_CTX.env.PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS = f;
        } else |_| {
            try out.print("error: invalid float value '{s}' for PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_WARN_EVALUATION_TIMEOUT")) |val| {
        if (std.fmt.parseFloat(f64, val)) |f| {
            RUN_CTX.env.PYDEVD_WARN_EVALUATION_TIMEOUT = f;
        } else |_| {
            try out.print("error: invalid float value '{s}' for PYDEVD_WARN_EVALUATION_TIMEOUT\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT")) |val| {
        if (parse_bool(val)) |b| {
            RUN_CTX.env.PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT = b;
        } else |_| {
            try out.print("error: invalid bool value '{s}' for PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_UNBLOCK_THREADS_TIMEOUT")) |val| {
        if (std.fmt.parseFloat(f64, val)) |f| {
            RUN_CTX.env.PYDEVD_UNBLOCK_THREADS_TIMEOUT = f;
        } else |_| {
            try out.print("error: invalid float value '{s}' for PYDEVD_UNBLOCK_THREADS_TIMEOUT\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_INTERRUPT_THREAD_TIMEOUT")) |val| {
        if (std.fmt.parseFloat(f64, val)) |f| {
            RUN_CTX.env.PYDEVD_INTERRUPT_THREAD_TIMEOUT = f;
        } else |_| {
            try out.print("error: invalid float value '{s}' for PYDEVD_INTERRUPT_THREAD_TIMEOUT\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_CONTAINER_BUCKET_SIZE")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_CONTAINER_BUCKET_SIZE = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_CONTAINER_BUCKET_SIZE\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_CONTAINER_NUMPY_MAX_ITEMS")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_CONTAINER_NUMPY_MAX_ITEMS = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_CONTAINER_NUMPY_MAX_ITEMS\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT")) |val| {
        if (std.fmt.parseFloat(f64, val)) |f| {
            RUN_CTX.env.PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT = f;
        } else |_| {
            try out.print("error: invalid float value '{s}' for PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_PANDAS_MAX_ROWS")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_PANDAS_MAX_ROWS = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_PANDAS_MAX_ROWS\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_PANDAS_MAX_COLS")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_PANDAS_MAX_COLS = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_PANDAS_MAX_COLS\n", .{val});
        }
    }
    if (env_map.get("PYDEVD_PANDAS_MAX_COLWIDTH")) |val| {
        if (std.fmt.parseInt(i64, val, 10)) |n| {
            RUN_CTX.env.PYDEVD_PANDAS_MAX_COLWIDTH = n;
        } else |_| {
            try out.print("error: invalid int value '{s}' for PYDEVD_PANDAS_MAX_COLWIDTH\n", .{val});
        }
    }
}

fn build_spawn_argv(al: std.mem.Allocator) ![]const []const u8 {
    var argv: std.ArrayList([]const u8) = .empty;

    switch (RUN_CTX.args_opt.vm_type orelse .python) {
        .python => try argv.append(al, "python"),
        .jython => try argv.append(al, "jython"),
    }

    for (obligatory_run_arguments) |arg| try argv.append(al, arg);

    try argv.append(al, "--port");
    try argv.append(al, try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.args_opt.port}));
    try argv.append(al, "--ppid");
    try argv.append(al, try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.args_opt.ppid}));

    if (RUN_CTX.args_opt.preimport) |val| {
        try argv.append(al, "--preimport");
        try argv.append(al, val);
    }
    if (RUN_CTX.args_opt.log_file) |val| {
        try argv.append(al, "--log-file");
        try argv.append(al, val);
    }
    if (RUN_CTX.args_opt.log_level != .critical) {
        try argv.append(al, "--log-level");
        try argv.append(al, @tagName(RUN_CTX.args_opt.log_level));
    }
    if (RUN_CTX.args_opt.qt_support != .auto) {
        try argv.append(al, "--qt-support");
        try argv.append(al, @tagName(RUN_CTX.args_opt.qt_support));
    }
    if (RUN_CTX.args_opt.startup_msg) try argv.append(al, "--print-in-debugger-startup");
    if (RUN_CTX.args_opt.module)      try argv.append(al, "--module");
    if (RUN_CTX.args_opt.file) |val| {
        try argv.append(al, "--file");
        try argv.append(al, val);
    }

    for (RUN_CTX.args) |arg| try argv.append(al, arg);

    return argv.toOwnedSlice(al);
}

fn build_spawn_env(al: std.mem.Allocator, source_env: *const std.process.Environ.Map) !std.process.Environ.Map {
    var env_map = try source_env.clone(al);

    for (env_sanitize) |key| _ = env_map.swapRemove(key);

    if (RUN_CTX.env.PYDEVD_USE_SYS_MONITORING) |b|
        try env_map.put("PYDEVD_USE_SYS_MONITORING", if (b) "1" else "0");
    if (RUN_CTX.env.PYDEVD_USE_CYTHON) |b|
        try env_map.put("PYDEVD_USE_CYTHON", if (b) "1" else "0");
    if (RUN_CTX.env.PYDEVD_USE_FRAME_EVAL) |b|
        try env_map.put("PYDEVD_USE_FRAME_EVAL", if (b) "1" else "0");
    if (RUN_CTX.env.PYDEVD_DEBUG_FILE) |val|
        try env_map.put("PYDEVD_DEBUG_FILE", val);
    if (RUN_CTX.env.GEVENT_SUPPORT_NOT_SET_MSG) |val|
        try env_map.put("GEVENT_SUPPORT_NOT_SET_MSG", val);
    if (RUN_CTX.env.PYDEVD_LOAD_NATIVE_LIB) |val|
        try env_map.put("PYDEVD_LOAD_NATIVE_LIB", val);

    try env_map.put("PYDEVD_LOG_TIME",
        if (RUN_CTX.env.PYDEVD_LOG_TIME) "1" else "0");
    try env_map.put("GEVENT_SUPPORT",
        if (RUN_CTX.env.GEVENT_SUPPORT) "1" else "0");
    try env_map.put("GEVENT_SHOW_PAUSED_GREENLETS",
        if (RUN_CTX.env.GEVENT_SHOW_PAUSED_GREENLETS) "1" else "0");
    try env_map.put("PYDEVD_DISABLE_FILE_VALIDATION",
        if (RUN_CTX.env.PYDEVD_DISABLE_FILE_VALIDATION) "1" else "0");
    try env_map.put("PYDEVD_LOAD_VALUES_ASYNC",
        if (RUN_CTX.env.PYDEVD_LOAD_VALUES_ASYNC) "1" else "0");
    try env_map.put("PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE",
        if (RUN_CTX.env.PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE) "1" else "0");
    try env_map.put("PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT",
        if (RUN_CTX.env.PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT) "1" else "0");

    try env_map.put("PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS}));
    try env_map.put("PYDEVD_WARN_EVALUATION_TIMEOUT",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_WARN_EVALUATION_TIMEOUT}));
    try env_map.put("PYDEVD_UNBLOCK_THREADS_TIMEOUT",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_UNBLOCK_THREADS_TIMEOUT}));
    try env_map.put("PYDEVD_INTERRUPT_THREAD_TIMEOUT",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_INTERRUPT_THREAD_TIMEOUT}));
    try env_map.put("PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT}));
    try env_map.put("PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS}));
    try env_map.put("PYDEVD_CONTAINER_BUCKET_SIZE",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_CONTAINER_BUCKET_SIZE}));
    try env_map.put("PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS}));
    try env_map.put("PYDEVD_CONTAINER_NUMPY_MAX_ITEMS",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_CONTAINER_NUMPY_MAX_ITEMS}));
    try env_map.put("PYDEVD_PANDAS_MAX_ROWS",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_PANDAS_MAX_ROWS}));
    try env_map.put("PYDEVD_PANDAS_MAX_COLS",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_PANDAS_MAX_COLS}));
    try env_map.put("PYDEVD_PANDAS_MAX_COLWIDTH",
        try std.fmt.allocPrint(al, "{d}", .{RUN_CTX.env.PYDEVD_PANDAS_MAX_COLWIDTH}));

    return env_map;
}

pub fn main(init: std.process.Init) !void {
    std.debug.print("Starting\n", .{});
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();

    const stdout_handle = std.Io.File.stdout();
    const stdout_buf = try arena.allocator().alloc(u8, 512);
    const stdout_buffered = stdout_handle.writer(init.io, stdout_buf);
    var stdout = stdout_buffered.interface;

    const args = try process_args(arena.allocator(), &stdout, init.minimal.args);
    _ = args;
    try process_envs(&stdout, init.environ_map);
    const spawn_argv = try build_spawn_argv(arena.allocator());
    var spawn_env = try build_spawn_env(arena.allocator(), init.environ_map);
    defer spawn_env.deinit();

    const ttys = try openPty();
    defer _ = std.os.linux.close(ttys.master);
    defer _ = std.os.linux.close(ttys.slave);

    const spawn_options: std.process.SpawnOptions = .{
        .pgid = 0,
        .uid = null,
        .gid = null,
        .cwd = .inherit,
        .disable_aslr = false,
        .expand_arg0 = .no_expand,
        .start_suspended = false,
        .create_no_window = true,
        .progress_node = std.Progress.Node.none,
        .request_resource_usage_statistics = false,

        .environ_map = &spawn_env,
        .argv = spawn_argv,
        .stderr = .{ .file = .{ .handle = ttys.slave, .flags = .{ .nonblocking = false } } },
        .stdin  = .{ .file = .{ .handle = ttys.slave, .flags = .{ .nonblocking = false } } },
        .stdout = .{ .file = .{ .handle = ttys.slave, .flags = .{ .nonblocking = false } } },
    };

    var child = try std.process.spawn(init.io, spawn_options);
    defer child.kill(init.io);
}

