"""Gunicorn runtime fixes for Luces Paraguay.

Keeps the existing Render start command unchanged while applying a tiny
production-only patch to the Google Maps frontend before Gunicorn resolves the
WSGI application.
"""


def post_fork(server, worker):
    # Import in the worker (not the master) so we do not preload the SQL engine
    # or the heavy geospatial stores across a fork.
    import app_google_maps as google_app

    # Jinja treats ``{#`` as the start of a template comment. The compressed
    # mobile CSS in the first Google Maps build accidentally produced exactly
    # that sequence: ``@media(...){#app``. Add one harmless CSS space so the
    # template parses normally.
    bad = "@media(max-width:900px){#app"
    good = "@media(max-width:900px){ #app"
    if bad in google_app.GOOGLE_INDEX_HTML:
        google_app.GOOGLE_INDEX_HTML = google_app.GOOGLE_INDEX_HTML.replace(bad, good)
        worker.log.info("Patched Google Maps template Jinja/CSS delimiter collision")
