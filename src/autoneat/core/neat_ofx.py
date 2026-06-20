"""Neat Video OFX node management — add tool to comp, wire color wrap, fire button."""

from __future__ import annotations

from typing import Any, Dict, Optional

NEAT_REG_ID = "ofx.com.absoft.NeatVideo6.rs"

# ColorSpaceTransform tool — wraps Neat so it sees display-referred pixels
# for analysis instead of scene-linear AP0 (which renders near-black on any
# monitor and breaks Auto Profile). Round-trip preserves working space at
# MediaOut.
CST_TOOL_ID = "ColorSpaceTransform"
CST_IN_NAME = "NeatCstIn"
CST_OUT_NAME = "NeatCstOut"

# HDR mastering luminance → matching PQ-peak gamma FuID. Verified against
# Resolve 20 by inspecting comp.AddTool("ColorSpaceTransform") inputs.
PQ_GAMMA_BY_NITS = {
    300: "PQ300_GAMMA",
    500: "PQ500_GAMMA",
    800: "PQ800_GAMMA",
    1000: "PQ1000_GAMMA",
    2000: "PQ2000_GAMMA",
    3000: "PQ3000_GAMMA",
    4000: "PQ4000_GAMMA",
}


def _find_tool_by_id(comp: Any, tool_id: str) -> Optional[Any]:
    for tool in (comp.GetToolList(False) or {}).values():
        try:
            if tool.ID == tool_id:
                return tool
        except Exception:
            continue
    return None


def _find_tool_by_name(comp: Any, name: str) -> Optional[Any]:
    try:
        return comp.FindTool(name)
    except Exception:
        return None


def _connect_input(dst: Any, input_name: str, src: Any) -> bool:
    """Resolve has historically accepted multiple ConnectInput signatures.
    Try all three known shapes and report success/failure."""
    for args in ((input_name, src, "Output"), (input_name, src), (input_name, src, "MainOutput")):
        try:
            if dst.ConnectInput(*args):
                return True
        except TypeError:
            continue
        except Exception:
            continue
    return False


def detect_color_wrap(project: Any) -> Dict[str, Any]:
    """Inspect the project's color settings and decide whether to wrap Neat
    with CST tools. Returns a dict with either:

      ``{"applied": False, "skip_reason": "..."}``
      ``{"applied": True, "in_cs": ..., "out_gamma": ..., "nits": ..., "mode": ...}``

    Raises ``RuntimeError`` if the project is ACES + HDR but the mastering
    luminance is not one of the supported PQ peaks — we will not silently
    feed AP0 linear into Neat.
    """
    if project is None:
        return {"applied": False, "skip_reason": "no current project"}

    mode = (project.GetSetting("colorScienceMode") or "").lower()
    if mode not in ("acescc", "acescct"):
        return {"applied": False, "skip_reason": f"colorScienceMode={mode!r} (not ACES)"}

    nits_raw = project.GetSetting("hdrMasteringLuminanceMax") or "0"
    nits = int(float(nits_raw))
    pq_gamma = PQ_GAMMA_BY_NITS.get(nits)
    if not pq_gamma:
        raise RuntimeError(
            f"ACES project ({mode}) but hdrMasteringLuminanceMax={nits_raw!r} is not "
            f"in {sorted(PQ_GAMMA_BY_NITS)}. Set the project's mastering luminance "
            "or check 'Skip ACES color wrap' in the Options panel if the clip is "
            "already display-referred."
        )
    return {
        "applied": True,
        "in_cs": "ACES_COLORSPACE",
        "in_gamma": "LINEAR_GAMMA",
        "out_cs": "REC2020_COLORSPACE",
        "out_gamma": pq_gamma,
        "nits": str(nits),
        "mode": mode,
    }


def _ensure_cst(comp: Any, name: str, *, x: float, wrap_cfg: Dict[str, Any], forward: bool) -> Any:
    existing = _find_tool_by_name(comp, name)
    if existing is None:
        tool = comp.AddTool(CST_TOOL_ID, x, 0)
        if tool is None:
            raise RuntimeError(f"comp.AddTool({CST_TOOL_ID!r}) returned None for {name}")
        tool.SetAttrs({"TOOLS_Name": name})
    else:
        tool = existing

    cfg = (
        ("InputColorSpace", wrap_cfg["in_cs"]),
        ("InputGamma", wrap_cfg["in_gamma"]),
        ("OutputColorSpace", wrap_cfg["out_cs"]),
        ("OutputGamma", wrap_cfg["out_gamma"]),
    ) if forward else (
        ("InputColorSpace", wrap_cfg["out_cs"]),
        ("InputGamma", wrap_cfg["out_gamma"]),
        ("OutputColorSpace", wrap_cfg["in_cs"]),
        ("OutputGamma", wrap_cfg["in_gamma"]),
    )
    for key, value in cfg:
        tool.SetInput(key, value)
    # Tone mapping must stay off so the round-trip is lossless.
    tool.SetInput("ToneMapping", 0)
    return tool


def attach_neat_to_clip(
    item: Any,
    project: Any,
    *,
    reuse_existing: bool,
    no_color_wrap: bool,
) -> Dict[str, Any]:
    """Add (or find) Neat on the clip's Fusion comp, wire the color wrap if
    appropriate, fire the OFX ``Prepare Profile___`` ButtonControl to open
    Neat's main window, and return a status dict.

    Raises ``RuntimeError`` on any wiring or settings failure. There is no
    silent fallback to direct wiring on ACES + HDR projects.
    """
    if item.GetFusionCompCount():
        comp = item.GetFusionCompByIndex(1)
    else:
        comp = item.AddFusionComp()
    if comp is None:
        raise RuntimeError("Could not get/add Fusion comp on the target clip")

    neat = _find_tool_by_id(comp, NEAT_REG_ID) if reuse_existing else None
    if neat is None:
        neat = comp.AddTool(NEAT_REG_ID, 1, 0)
    if neat is None:
        raise RuntimeError(f"Could not add Neat OFX {NEAT_REG_ID!r}")

    if no_color_wrap:
        wrap_info: Dict[str, Any] = {"applied": False, "skip_reason": "no_color_wrap=True"}
        wrap_cfg = None
    else:
        wrap_info = detect_color_wrap(project)
        wrap_cfg = wrap_info if wrap_info.get("applied") else None

    cst_in = cst_out = None
    if wrap_cfg is not None:
        cst_in = _ensure_cst(comp, CST_IN_NAME, x=0.5, wrap_cfg=wrap_cfg, forward=True)
        cst_out = _ensure_cst(comp, CST_OUT_NAME, x=1.5, wrap_cfg=wrap_cfg, forward=False)

    media_in = _find_tool_by_name(comp, "MediaIn1")
    media_out = _find_tool_by_name(comp, "MediaOut1")
    if wrap_cfg is not None:
        # MediaIn1 → CstIn → Neat → CstOut → MediaOut1
        if media_in is not None:
            _connect_input(cst_in, "Input", media_in)
        _connect_input(neat, "Source", cst_in)
        _connect_input(cst_out, "Input", neat)
        if media_out is not None:
            _connect_input(media_out, "Input", cst_out)
    else:
        # Direct: MediaIn1 → Neat → MediaOut1
        if media_in is not None:
            _connect_input(neat, "Source", media_in)
        if media_out is not None:
            _connect_input(media_out, "Input", neat)

    comp.SetActiveTool(neat)

    # Open the Neat UI window via the OFX ButtonControl. Cycling 0 → 1
    # forces an inputChanged callback even if a prior run left the button
    # latched at 1. If either SetInput raises, we let it bubble up.
    neat.SetInput("Prepare Profile___", 0.0)
    neat.SetInput("Prepare Profile___", 1.0)

    return {
        "tool": getattr(neat, "Name", "Reduce Noise v6"),
        "color_wrap": wrap_info,
    }
