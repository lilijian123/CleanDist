// 只读枚举 Win32 文件元数据，输出带帧结构的二进制流，不读取文件内容。
using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

internal static class Scan {
    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime { public uint Low, High; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct FindData {
        public uint Attributes;
        public FileTime Created, Accessed, Modified;
        public uint SizeHigh, SizeLow, Reserved0, Reserved1;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)] public string Alternate;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFileAttributesW(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr FindFirstFileExW(string path, int level, out FindData data,
                                                int search, IntPtr filter, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FindNextFileW(IntPtr handle, out FindData data);
    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FindClose(IntPtr handle);

    private static readonly IntPtr InvalidHandle = new IntPtr(-1);
    private static readonly object OutputLock = new object();
    private static readonly BlockingCollection<string> Directories = new BlockingCollection<string>();
    // 跳过重解析点、设备及云端或离线占位文件，避免遍历或触发内容下载。
    private const uint SkipAttributes = 0x400 | 0x40 | 0x1000 | 0x40000 | 0x400000;
    private static BinaryWriter Output;
    private static string Root;
    private static long PendingDirectories = 1, LastFlush;
    private static Exception WorkerError;

    private static void WriteText(BinaryWriter writer, string value) {
        byte[] bytes = Encoding.UTF8.GetBytes(value);
        writer.Write(bytes.Length);
        writer.Write(bytes);
    }
    private static ulong PackFileTime(FileTime value) { return ((ulong)value.High << 32) | value.Low; }
    private static void FlushBuffer(MemoryStream buffer) {
        if (buffer.Length == 0) return;
        lock (OutputLock) {
            Output.Write(buffer.GetBuffer(), 0, (int)buffer.Length);
            long now = Stopwatch.GetTimestamp();
            if (now - LastFlush > Stopwatch.Frequency / 5) { Output.Flush(); LastFlush = now; }
        }
        buffer.SetLength(0);
        buffer.Position = 0;
    }
    private static void WriteError(BinaryWriter writer, string relative, int code) {
        writer.Write((byte)'E'); WriteText(writer, relative); writer.Write(code);
    }

    private static void ScanDirectory(string relative) {
        string path = Root + relative.Replace('/', '\\');
        using (var buffer = new MemoryStream(65536))
        using (var writer = new BinaryWriter(buffer, Encoding.UTF8)) {
            uint attributes = GetFileAttributesW(path);
            if (attributes == uint.MaxValue) { WriteError(writer, relative, Marshal.GetLastWin32Error()); FlushBuffer(buffer); return; }
            if ((attributes & SkipAttributes) != 0) { writer.Write((byte)'K'); FlushBuffer(buffer); return; }
            if ((attributes & 0x10) == 0) { WriteError(writer, relative, 267); FlushBuffer(buffer); return; }
            writer.Write((byte)'D'); WriteText(writer, relative);
            FindData data;
            string pattern = path.TrimEnd('\\') + "\\*";
            IntPtr handle = FindFirstFileExW(pattern, 1, out data, 0, IntPtr.Zero, 2);
            if (handle == InvalidHandle) {
                int code = Marshal.GetLastWin32Error();
                if (code == 87 || code == 50) handle = FindFirstFileExW(pattern, 1, out data, 0, IntPtr.Zero, 0);
            }
            if (handle == InvalidHandle) {
                int code = Marshal.GetLastWin32Error();
                if (code != 2 && code != 18) WriteError(writer, relative, code);
                FlushBuffer(buffer); return;
            }
            try {
                do {
                    if (data.Name == "." || data.Name == "..") continue;
                    if ((data.Attributes & SkipAttributes) != 0) { writer.Write((byte)'K'); continue; }
                    string child = relative.Length == 0 ? data.Name : relative + "/" + data.Name;
                    if ((data.Attributes & 0x10) != 0) {
                        Interlocked.Increment(ref PendingDirectories);
                        Directories.Add(child);
                    } else {
                        writer.Write((byte)'F'); WriteText(writer, child);
                        writer.Write(((ulong)data.SizeHigh << 32) | data.SizeLow);
                        writer.Write(PackFileTime(data.Modified)); writer.Write(PackFileTime(data.Created));
                    }
                    if (buffer.Length >= 65536) FlushBuffer(buffer);
                } while (FindNextFileW(handle, out data));
                int error = Marshal.GetLastWin32Error();
                if (error != 18) WriteError(writer, relative, error);
            } finally { FindClose(handle); }
            FlushBuffer(buffer);
        }
    }

    private static void ScanWorker() {
        try {
            foreach (string relative in Directories.GetConsumingEnumerable()) {
                try { ScanDirectory(relative); }
                finally { if (Interlocked.Decrement(ref PendingDirectories) == 0) Directories.CompleteAdding(); }
            }
        } catch (Exception error) {
            WorkerError = error;
            Directories.CompleteAdding();
        }
    }

    public static int Main(string[] args) {
        try {
            if (args.Length != 1) return 2;
            Root = Path.GetFullPath(args[0]).TrimEnd('\\') + "\\";
            if (!Root.StartsWith(@"\\?\")) Root = Root.StartsWith(@"\\") ? @"\\?\UNC\" + Root.Substring(2) : @"\\?\" + Root;
            Output = new BinaryWriter(new BufferedStream(Console.OpenStandardOutput(), 1048576), Encoding.UTF8);
            Output.Write(Encoding.ASCII.GetBytes("RSC1")); Output.Flush();
            Directories.Add("");
            int workerCount = Math.Min(8, Math.Max(2, Environment.ProcessorCount));
            var workers = new Thread[workerCount];
            for (int index = 0; index < workerCount; index++) { workers[index] = new Thread(ScanWorker); workers[index].Start(); }
            foreach (Thread worker in workers) worker.Join();
            Output.Flush();
            return WorkerError == null ? 0 : 2;
        } catch { return 2; }
    }
}
