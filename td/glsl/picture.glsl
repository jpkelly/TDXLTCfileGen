// LTC File Generator: composes the timecode picture.
// Inputs (alpha only; all at output resolution):
//   0 main readout   1 secondary counter   2 countdown   3 info caption
// Every value that changes per frame arrives as a uniform set by the
// extension, so the picture for frame i depends on nothing but i.

uniform vec4 uBg;        // background colour (rgb)
uniform vec4 uFg;        // text / marker colour (rgb)
uniform vec4 uBarcode;   // x: on, y: frame index, z: strip height (px)
uniform vec4 uMarker;    // x: on, y: active slot, z: slot count, w: band height (px)
uniform vec4 uInfo;      // x: caption weight (0-1)
uniform vec4 uAlpha;     // x: clear background (HAP Alpha / ProRes 4444, not on flash frames)

out vec4 fragColor;

void main()
{
    vec2 res = uTDOutputInfo.res.zw;
    vec2 px = vUV.st * res;                       // origin bottom-left, in pixels
    vec3 col = uBg.rgb;

    float a = 0.0;
    for (int k = 0; k < 3; k++)
        a = max(a, texture(sTD2DInputs[k], vUV.st).a);
    col = mix(col, uFg.rgb, a);
    float ai = texture(sTD2DInputs[3], vUV.st).a * uInfo.x;
    col = mix(col, uFg.rgb, ai);
    // Weight of the text colour over the background: the alpha when clear.
    float t = mix(a, 1.0, ai);

    // Frame marker: one slot per frame of the second along the bottom.
    if (uMarker.x > 0.5 && px.y < uMarker.w) {
        float slotW = res.x / uMarker.z;
        float s = floor(px.x / slotW);
        float fx = px.x - s * slotW;
        bool inner = fx >= 1.0 && fx < slotW - 1.0 && px.y >= 2.0 && px.y < uMarker.w - 2.0;
        if (inner) {
            bool on = abs(s - uMarker.y) < 0.5;
            col = on ? uFg.rgb : mix(uBg.rgb, uFg.rgb, 0.2);
            t = on ? 1.0 : 0.2;
        }
    }

    // Clear background: premultiplied text colour (as TouchDesigner plays it back).
    vec4 outc = uAlpha.x > 0.5 ? vec4(uFg.rgb * t, t) : vec4(col, 1.0);

    // Debug barcode strip across the top: frame index in 24 bars, LSB at
    // left; upper half = bits, lower half = inverted bits (tools/av_check.py).
    if (uBarcode.x > 0.5 && px.y >= res.y - uBarcode.z) {
        int f = int(uBarcode.y + 0.5);
        int bar = int(floor(vUV.s * 24.0));
        float bitv = float((f >> bar) & 1);
        bool upper = px.y >= res.y - 0.5 * uBarcode.z;
        outc = vec4(vec3(upper ? bitv : 1.0 - bitv), 1.0);
    }

    fragColor = TDOutputSwizzle(outc);
}
