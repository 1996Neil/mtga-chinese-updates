"""TMP v2 parser reused from historical global-fallback builder; fail closed on layout drift."""
import struct

class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def number(self, fmt):
        val = struct.unpack_from('<' + fmt, self.data, self.pos)[0]
        self.pos += struct.calcsize(fmt)
        return val

    def align(self):
        self.pos = (self.pos + 3) & ~3

    def boolean(self):
        val = self.number('B')
        assert val in (0, 1)
        self.align()
        return bool(val)

    def text(self):
        n = self.number('i')
        assert n >= 0
        result = self.data[self.pos:self.pos + n].decode('utf-8')
        self.pos += n
        self.align()
        return result

    def pointer(self):
        return {'m_FileID': self.number('i'), 'm_PathID': self.number('q')}

    def pointers(self):
        n = self.number('i')
        assert 0 <= n < 1000
        return [self.pointer() for _ in range(n)]


def parse_settings(data):
    """Current assetVersion 2 layout, validated against complete payload and neighbors."""
    r = Reader(data)
    fields = {'m_GameObject': r.pointer(), 'm_Enabled': r.boolean(), 'm_Script': r.pointer(), 'm_Name': r.text()}
    fields['assetVersion'] = r.text()
    assert fields['assetVersion'] == '2'
    fields['m_TextWrappingMode'] = r.number('i')
    fields['m_enableKerning'] = r.boolean()
    fields['m_ActiveFontFeatures'] = [r.number('I') for _ in range(r.number('i'))]
    # Current installed version predates the later master branch m_UseAdvancedText field.
    for name in ('m_enableExtraPadding', 'm_enableTintAllSprites', 'm_enableParseEscapeCharacters', 'm_EnableRaycastTarget', 'm_GetFontFeaturesAtRuntime'):
        fields[name] = r.boolean()
    fields['m_missingGlyphCharacter'] = r.number('i')
    fields['m_ClearDynamicDataOnBuild'] = r.boolean()
    fields['m_warningsDisabled'] = r.boolean()
    fields['m_defaultFontAsset'] = r.pointer()
    fields['m_defaultFontAssetPath'] = r.text()
    for name in ('m_defaultFontSize', 'm_defaultAutoSizeMinRatio', 'm_defaultAutoSizeMaxRatio'):
        fields[name] = r.number('f')
    fields['m_defaultTextMeshProTextContainerSize'] = [r.number('f'), r.number('f')]
    fields['m_defaultTextMeshProUITextContainerSize'] = [r.number('f'), r.number('f')]
    fields['m_autoSizeTextContainer'] = r.boolean()
    fields['m_IsTextObjectScaleStatic'] = r.boolean()
    fallback_offset = r.pos
    fields['m_fallbackFontAssets'] = r.pointers()
    fallback_end = r.pos
    fields['m_matchMaterialPreset'] = r.boolean()
    fields['m_HideSubTextObjects'] = r.boolean()
    fields['m_defaultSpriteAsset'] = r.pointer()
    fields['m_defaultSpriteAssetPath'] = r.text()
    fields['m_enableEmojiSupport'] = r.boolean()
    fields['m_MissingCharacterSpriteUnicode'] = r.number('I')
    fields['m_EmojiFallbackTextAssets'] = r.pointers()
    fields['m_defaultColorGradientPresetsPath'] = r.text()
    fields['m_defaultStyleSheet'] = r.pointer()
    fields['m_StyleSheetsResourcePath'] = r.text()
    fields['m_leadingCharacters'] = r.pointer()
    fields['m_followingCharacters'] = r.pointer()
    fields['m_UseModernHangulLineBreakingRules'] = r.boolean()
    assert r.pos == len(data), (r.pos, len(data))
    return fields, fallback_offset, fallback_end
