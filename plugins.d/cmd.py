"""List available primary command names using only the core plugin API."""

from botcore.api import Command, Registration, Response


HEADER = "CMD | Help"


def command_pages(names, services):
    """Pack whole lines, recalculating when numbering reduces packet capacity."""
    total = 1
    while True:
        budget = services.packet_payload_bytes(total)
        header_bytes = len(HEADER.encode("utf-8"))
        if header_bytes > budget:
            raise ValueError("core.message_bytes is too small for the cmd header and numbering")
        pages, lines, size = [], [], header_bytes
        for name in names:
            width = 1 + len(name.encode("utf-8"))  # Newline plus the complete command.
            if header_bytes + width > budget:
                raise ValueError("core.message_bytes is too small for a complete cmd entry and header")
            if size + width > budget:
                pages.append(HEADER + "\n" + "\n".join(lines))
                lines, size = [], header_bytes
            lines.append(name)
            size += width
        pages.append(HEADER + ("\n" + "\n".join(lines) if lines else ""))
        if len(pages) <= total:
            return tuple(pages)
        total = len(pages)


def register(settings):
    async def handle(context):
        names = context.services.command_names(context.message)
        pages = command_pages(names, context.services)
        # These are literal command spellings, including any @ characters.
        return Response(pages, normalize_mentions=False)

    return Registration(commands=(Command("cmd", handle, help="List available commands."),))
