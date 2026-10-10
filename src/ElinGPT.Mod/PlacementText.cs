namespace ElinGPT.Mod
{
    internal static class PlacementText
    {
        // Wall/fence variants 0 and 1 extend along these map axes. Other variants
        // are shapes, not compass headings; retain their native integer unchanged.
        public static string? WallAxis(bool wallOrFence, int variant) =>
            !wallOrFence ? null : variant == 0 ? "x" : variant == 1 ? "z" : null;
    }
}
