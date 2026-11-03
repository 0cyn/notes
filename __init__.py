"""Binary Ninja's plugin entry point. """

from importlib.util import find_spec

if find_spec("binaryninja") is not None:
    import binaryninja

    from .src.mcp import register_tools
    from .src.merge import register_merge
    from .src.settings import normalize_view_settings, register_settings

    register_settings()
    register_tools()
    register_merge()
    binaryninja.BinaryViewType.add_binaryview_finalized_event(normalize_view_settings)

    if binaryninja.core_ui_enabled():
        from .src.ui.integration import register_ui

        register_ui()
