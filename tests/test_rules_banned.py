from conftest import codes, lint

from lintex import Severity


def test_sql_interpolation_variants():
    src = """
        def q(cur, uid, name):
            cur.execute(f"SELECT * FROM users WHERE id = {uid}")
            cur.execute("SELECT * FROM users WHERE name = '%s'" % name)
            cur.execute("DELETE FROM users WHERE id = {}".format(uid))
            cur.execute("UPDATE users SET name = '" + name + "' WHERE id = 1")
    """
    vs = lint(src)
    assert [(v.code, v.line) for v in vs] == [("LX201", n) for n in (3, 4, 5, 6)]
    assert "bound parameters" in vs[0].fix


def test_sql_safe_patterns_pass():
    src = """
        def q(cur, uid, ids):
            cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
            placeholders = ",".join("?" * len(ids))
            cur.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", ids)
    """
    assert codes(src) == []


def test_prose_is_not_sql():
    assert codes('def f(n):\n    return f"Updated {n} items"\n') == []


def test_raw_sql_opt_in_with_custom_hint():
    src = 'def f(cur):\n    cur.execute("SELECT id FROM users")\n'
    assert codes(src) == []
    cfg = {"enable": ["raw-sql"], "rules": {"raw-sql": {"hint": "Use app.db.repo"}}}
    [v] = lint(src, **cfg)
    assert v.code == "LX202" and v.fix == "Use app.db.repo"


def test_dangerous_calls():
    src = """
        import os, pickle, subprocess, yaml, requests
        eval(data)
        exec(code)
        pickle.loads(blob)
        yaml.load(text)
        yaml.load(text, Loader=yaml.SafeLoader)
        os.system("ls " + d)
        subprocess.run(cmd, shell=True)
        subprocess.run("ls -l", shell=True)
        requests.get(url, verify=False)
    """
    vs = lint(src)
    assert [(v.code, v.line) for v in vs] == [("LX203", n) for n in (3, 4, 5, 6, 8, 9, 10, 11)]
    assert vs[6].severity is Severity.WARNING  # literal command with shell=True


def test_insecure_file_operations():
    src = """
        import os, tarfile, tempfile
        tempfile.mktemp()
        os.chmod(p, 0o777)
        os.chmod(p, 0o644)
        os.makedirs(d, mode=0o777, exist_ok=True)
        with tarfile.open(f) as tar:
            tar.extractall(dest)
            tar.extractall(dest, filter="data")
        open("/tmp/output.txt", "w")
    """
    vs = lint(src)
    assert [(v.code, v.line) for v in vs] == [("LX204", n) for n in (3, 4, 6, 8, 10)]


def test_project_banned_apis():
    cfg = {
        "rules": {
            "banned-api": {
                "apis": {
                    "requests": "Use app.http.client.",
                    "print": "Use the logger.",
                    "os.system": "Use app.proc.run.",
                }
            }
        }
    }
    src = """
        import os
        import requests
        from requests import get
        print("hi")
        os.system("x")
        os.path.join("a")
    """
    vs = [v for v in lint(src, **cfg) if v.code == "LX205"]
    assert [v.line for v in vs] == [3, 4, 5, 6]
    assert vs[0].fix == "Use app.http.client."
    assert "`print` is banned" in vs[2].message
