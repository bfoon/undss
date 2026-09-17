import os


class CupsUnavailable(RuntimeError):
    pass


def _connection():
    try:
        import cups
    except ImportError as exc:
        raise CupsUnavailable(
            "pycups is not installed in the web container."
        ) from exc

    server = os.getenv("UNPASS_CUPS_SERVER", "").strip()
    port = os.getenv("UNPASS_CUPS_PORT", "").strip()

    if server:
        cups.setServer(server)
    if port:
        try:
            cups.setPort(int(port))
        except Exception:
            pass

    return cups.Connection()


def server_status():
    try:
        conn = _connection()
        return {
            "available": True,
            "printers": conn.getPrinters(),
            "error": "",
        }
    except Exception as exc:
        return {
            "available": False,
            "printers": {},
            "error": str(exc),
        }


def printer_jobs(queue_name):
    conn = _connection()
    jobs = conn.getJobs(which_jobs="all", my_jobs=False)
    if not queue_name:
        return jobs

    filtered = {}
    for job_id, job in jobs.items():
        printer_uri = str(job.get("job-printer-uri") or "")
        printer_name = str(job.get("printer-name") or "")
        if queue_name in printer_uri or queue_name == printer_name:
            filtered[job_id] = job
    return filtered


def pause_printer(queue_name):
    _connection().disablePrinter(queue_name)


def resume_printer(queue_name):
    conn = _connection()
    conn.enablePrinter(queue_name)
    try:
        conn.acceptJobs(queue_name)
    except Exception:
        pass


def cancel_job(job_id):
    _connection().cancelJob(int(job_id), purge_job=True)
