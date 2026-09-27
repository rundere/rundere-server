// Rundere design tokens — Jetpack Compose Material 3. Mirror of design/tokens/tokens.json; change both together.
//
// Fonts: Outfit + Figtree via downloadable Google Fonts (androidx.compose.ui:ui-text-google-fonts),
// JetBrains Mono for codes. Dynamic colour is OFF by default; expose it as a user setting.

package app.rundere.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

val RundereDark = darkColorScheme(
    primary = Color(0xFF7FB2FF),
    onPrimary = Color(0xFF0A1624),
    primaryContainer = Color(0xFF16304A),
    onPrimaryContainer = Color(0xFFC7DDFF),
    secondary = Color(0xFFAAB6C4),
    onSecondary = Color(0xFF0A1624),
    secondaryContainer = Color(0xFF22405F),
    onSecondaryContainer = Color(0xFFC7DDFF),
    tertiary = Color(0xFFF2B544),
    onTertiary = Color(0xFF0A1624),
    error = Color(0xFFF2766B),
    onError = Color(0xFF0A1624),
    background = Color(0xFF0A1624),
    onBackground = Color(0xFFE6EAF0),
    surface = Color(0xFF0A1624),
    onSurface = Color(0xFFE6EAF0),
    onSurfaceVariant = Color(0xFFAAB6C4),
    surfaceContainerLowest = Color(0xFF070F19),
    surfaceContainerLow = Color(0xFF0C1B2C),
    surfaceContainer = Color(0xFF0F2135),
    surfaceContainerHigh = Color(0xFF16304A),
    surfaceContainerHighest = Color(0xFF22405F),
    outline = Color(0xFF4F6F94),
    outlineVariant = Color(0xFF22405F),
)

val RundereLight = lightColorScheme(
    primary = Color(0xFF1F57B0),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFE3EEFF),
    onPrimaryContainer = Color(0xFF0F2135),
    secondary = Color(0xFF44536A),
    onSecondary = Color(0xFFFFFFFF),
    secondaryContainer = Color(0xFFE6EAF0),
    onSecondaryContainer = Color(0xFF0F2135),
    tertiary = Color(0xFF8A5C08),
    onTertiary = Color(0xFFFFFFFF),
    error = Color(0xFFC23B30),
    onError = Color(0xFFFFFFFF),
    background = Color(0xFFF4F6F9),
    onBackground = Color(0xFF0F2135),
    surface = Color(0xFFF4F6F9),
    onSurface = Color(0xFF0F2135),
    onSurfaceVariant = Color(0xFF44536A),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFFFFFFF),
    surfaceContainer = Color(0xFFFFFFFF),
    surfaceContainerHigh = Color(0xFFEEF1F5),
    surfaceContainerHighest = Color(0xFFE6EAF0),
    outline = Color(0xFF8594A6),
    outlineVariant = Color(0xFFCBD3DD),
)

val RundereShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(28.dp),
)

/** Type roles → families (see design/README.md for the full scale):
 *  display*, headline*, titleLarge → Outfit (Light 300 for display/headlineLarge)
 *  titleMedium/Small, body*, label* → Figtree
 *  flight codes, IATA, confirmation refs → JetBrains Mono 14/20 */

@Composable
fun RundereTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (darkTheme) RundereDark else RundereLight,
        shapes = RundereShapes,
        content = content,
    )
}
