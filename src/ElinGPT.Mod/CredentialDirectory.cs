using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;

namespace ElinGPT.Mod
{
    internal static class CredentialDirectory
    {
        public static void CreatePrivate(string path)
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT)
                throw new PlatformNotSupportedException("ElinGPT credential permissions require Windows.");
            var directory = Directory.CreateDirectory(path);
            if ((directory.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new IOException("The credential directory must not be a link or junction.");
            string userSid = CurrentUserSid();
            IntPtr descriptor = IntPtr.Zero;
            try
            {
                // Mono does not implement WindowsIdentity.User or managed ACL setters.
                if (!ConvertStringSecurityDescriptorToSecurityDescriptor(
                    "D:P(A;OICI;FA;;;" + userSid + ")", 1, out descriptor, out _))
                    throw NativeError("Create credential directory security descriptor");
                if (!SetFileSecurity(path, 0x80000004u, descriptor))
                    throw NativeError("Apply private credential directory permissions");
            }
            finally { if (descriptor != IntPtr.Zero) LocalFree(descriptor); }
        }

        private static string CurrentUserSid()
        {
            if (!OpenProcessToken(GetCurrentProcess(), 0x0008, out IntPtr token))
                throw NativeError("Read current process security token");
            IntPtr buffer = IntPtr.Zero;
            IntPtr text = IntPtr.Zero;
            try
            {
                if (GetTokenInformation(token, 1, IntPtr.Zero, 0, out int length) ||
                    Marshal.GetLastWin32Error() != 122 || length <= 0 || length > 65536)
                    throw NativeError("Determine current-user token size");
                buffer = Marshal.AllocHGlobal(length);
                if (!GetTokenInformation(token, 1, buffer, length, out _))
                    throw NativeError("Read current-user token");
                if (!ConvertSidToStringSid(Marshal.ReadIntPtr(buffer), out text))
                    throw NativeError("Read current-user SID");
                return Marshal.PtrToStringUni(text) ??
                    throw new InvalidOperationException("Windows returned an empty current-user SID.");
            }
            finally
            {
                if (text != IntPtr.Zero) LocalFree(text);
                if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer);
                CloseHandle(token);
            }
        }

        private static Win32Exception NativeError(string operation) =>
            new Win32Exception(Marshal.GetLastWin32Error(), operation + " failed.");

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetTokenInformation(IntPtr token, int informationClass,
            IntPtr information, int length, out int returnLength);
        [DllImport("advapi32.dll", EntryPoint = "ConvertSidToStringSidW", ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ConvertSidToStringSid(IntPtr sid, out IntPtr text);
        [DllImport("advapi32.dll", EntryPoint = "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            ExactSpelling = true, CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string text,
            uint revision, out IntPtr descriptor, out uint size);
        [DllImport("advapi32.dll", EntryPoint = "SetFileSecurityW",
            ExactSpelling = true, CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetFileSecurity(string path, uint information, IntPtr descriptor);
    }
}
