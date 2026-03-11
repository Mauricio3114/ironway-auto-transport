import os
import io
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin,
    login_user, login_required, logout_user,
    current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth


# =========================
# APP CONFIG
# =========================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "ironway.db")

app = Flask(__name__)
app.secret_key = "ironway-secret"

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# =========================
# MODELS
# =========================
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    senha = db.Column(db.String(255), nullable=False)
    tipo = db.Column(db.String(40), nullable=False, default="admin")  # admin

    def __repr__(self):
        return f"<User {self.email}>"


class MonthlyConfig(db.Model):
    """
    Config do mês: define se o mês tem 4 ou 5 semanas pro rateio.
    """
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    weeks_in_month = db.Column(db.Integer, nullable=False, default=4)  # 4 ou 5
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("year", "month", name="uq_month_config_year_month"),
    )


class MonthlyFixedCost(db.Model):
    """
    Despesas mensais fixas (por mês/ano).
    """
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)

    name = db.Column(db.String(120), nullable=False)
    amount_monthly = db.Column(db.Float, nullable=False, default=0.0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WeeklyClose(db.Model):
    """
    Fechamento da semana (manual).
    Receita total => % motoristas/dispatcher calculados em cima dela.
    """
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)

    week_no = db.Column(db.Integer, nullable=False, index=True)  # 1..5

    period_start = db.Column(db.String(20), nullable=True)  # "2026-02-01" (opcional)
    period_end = db.Column(db.String(20), nullable=True)

    revenue = db.Column(db.Float, nullable=False, default=0.0)

    fuel = db.Column(db.Float, nullable=False, default=0.0)
    extra_expenses = db.Column(db.Float, nullable=False, default=0.0)

    cargo_insurance_weekly = db.Column(db.Float, nullable=False, default=250.0)

    miles = db.Column(db.Float, nullable=False, default=0.0)
    gallons = db.Column(db.Float, nullable=False, default=0.0)

    notes = db.Column(db.Text, nullable=True)

    payment_status = db.Column(db.String(20), nullable=False, default="pendente")  # pendente/pago

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("year", "month", "week_no", name="uq_weekclose_year_month_week"),
    )


# =========================
# AUTH / HELPERS
# =========================
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login"))
        if getattr(current_user, "tipo", "") != "admin":
            flash("Somente ADMIN pode acessar.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def _to_int(v, default):
    try:
        return int(v)
    except Exception:
        return default


def _to_float(v, default=0.0):
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "")
        if s == "":
            return default
        return float(s)
    except Exception:
        return default


def month_label(y, m):
    return f"{y}-{m:02d}"


def get_or_create_month_config(year, month):
    cfg = MonthlyConfig.query.filter_by(year=year, month=month).first()
    if not cfg:
        cfg = MonthlyConfig(year=year, month=month, weeks_in_month=4)
        db.session.add(cfg)
        db.session.commit()
    return cfg


def get_fixed_costs_sum(year, month):
    rows = MonthlyFixedCost.query.filter_by(year=year, month=month).all()
    total = float(sum((r.amount_monthly or 0) for r in rows))
    return total, rows


# ✅ ESTA FUNÇÃO ESTAVA FALTANDO (ERRO DO TEU PRINT)
def compute_week_calc(w: WeeklyClose, weeks_in_month: int, fixed_month_total: float):
    revenue = float(w.revenue or 0)
    fuel = float(w.fuel or 0)
    extra = float(w.extra_expenses or 0)
    cargo = float(w.cargo_insurance_weekly or 0)

    driver = revenue * 0.30
    dispatcher = revenue * 0.12

    weeks_in_month = 4 if weeks_in_month not in (4, 5) else weeks_in_month
    fixed_week = fixed_month_total / weeks_in_month if weeks_in_month > 0 else 0.0

    total_expenses = fuel + extra + cargo + driver + dispatcher + fixed_week
    net = revenue - total_expenses

    mpg = None
    if (w.gallons or 0) > 0:
        mpg = (w.miles or 0) / (w.gallons or 0)

    return {
        "revenue": revenue,
        "fuel": fuel,
        "extra": extra,
        "cargo": cargo,
        "driver": driver,
        "dispatcher": dispatcher,
        "fixed_week": fixed_week,
        "total_expenses": total_expenses,
        "net": net,
        "mpg": mpg,
    }


def compute_month_aggregate(year: int, month: int):
    cfg = get_or_create_month_config(year, month)
    fixed_month_total, _ = get_fixed_costs_sum(year, month)

    weeks_rows = WeeklyClose.query.filter_by(year=year, month=month).order_by(WeeklyClose.week_no.asc()).all()

    totals = {
        "revenue": 0.0,
        "expenses": 0.0,
        "net": 0.0,
        "fuel": 0.0,
        "extra": 0.0,
        "cargo": 0.0,
        "driver": 0.0,
        "dispatcher": 0.0,
        "fixed_week_total": 0.0,
    }

    mpg_sum = 0.0
    mpg_n = 0

    week_cards = []
    for w in weeks_rows:
        calc = compute_week_calc(w, cfg.weeks_in_month, fixed_month_total)

        totals["revenue"] += calc["revenue"]
        totals["expenses"] += calc["total_expenses"]
        totals["net"] += calc["net"]
        totals["fuel"] += calc["fuel"]
        totals["extra"] += calc["extra"]
        totals["cargo"] += calc["cargo"]
        totals["driver"] += calc["driver"]
        totals["dispatcher"] += calc["dispatcher"]
        totals["fixed_week_total"] += calc["fixed_week"]

        if calc["mpg"] is not None:
            mpg_sum += float(calc["mpg"])
            mpg_n += 1

        week_cards.append({
            "week_no": int(w.week_no),
            "label": f"Sem {w.week_no}",
            "period": f"{w.period_start or '--'} → {w.period_end or '--'}",
            "payment_status": w.payment_status,
            "calc": calc,
        })

    mpg_month = (mpg_sum / mpg_n) if mpg_n > 0 else None
    return cfg, fixed_month_total, weeks_rows, week_cards, totals, mpg_month


def _money(v):
    try:
        return f"${float(v or 0):,.2f}"
    except Exception:
        return "$0.00"


def _safe_text(s):
    return (s or "").strip()


# =========================
# PDF PREMIUM ENGINE
# =========================

PDF_BRAND = "IRONWAY AUTO TRANSPORT"
PDF_SUBTITLE_WEEK = "Weekly Close Report"
PDF_SUBTITLE_MONTH = "Monthly Financial Report"

PDF_BG = colors.HexColor("#070b16")
PDF_PANEL = colors.HexColor("#0b1222")
PDF_PANEL_2 = colors.HexColor("#0f1a33")
PDF_LINE = colors.HexColor("#24314d")
PDF_TEXT = colors.HexColor("#e5e7eb")
PDF_MUTED = colors.HexColor("#9aa6bd")
PDF_GREEN = colors.HexColor("#22c55e")
PDF_RED = colors.HexColor("#ef4444")


def _fmt_money(v):
    try:
        return f"${float(v or 0):,.2f}"
    except Exception:
        return "$0.00"


def _fmt_num(v, nd=2):
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return "--"


def _draw_header(c: canvas.Canvas, title: str, subtitle: str, meta_left: str, meta_right: str):
    w, h = letter

    c.setFillColor(PDF_BG)
    c.rect(0, 0, w, h, fill=1, stroke=0)

    c.setFillColor(PDF_PANEL)
    c.rect(0, h - 110, w, 110, fill=1, stroke=0)

    c.setFillColor(PDF_GREEN)
    c.rect(0, h - 110, w, 4, fill=1, stroke=0)

    c.setFillColor(PDF_TEXT)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(36, h - 48, title)

    c.setFont("Helvetica", 11)
    c.setFillColor(PDF_MUTED)
    c.drawString(36, h - 68, subtitle)

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawString(36, h - 92, meta_left)
    c.drawRightString(w - 36, h - 92, meta_right)

    return h - 130


def _draw_footer(c: canvas.Canvas, page_num: int):
    w, _ = letter
    c.setFont("Helvetica", 9)
    c.setFillColor(PDF_MUTED)
    c.drawString(36, 28, f"{PDF_BRAND} • Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    c.drawRightString(w - 36, 28, f"Page {page_num}")


def _panel(c: canvas.Canvas, x, y, w, h, title=None):
    c.setFillColor(PDF_PANEL_2)
    c.setStrokeColor(PDF_LINE)
    c.setLineWidth(1)
    c.roundRect(x, y, w, h, 10, fill=1, stroke=1)

    if title:
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(PDF_TEXT)
        c.drawString(x + 14, y + h - 20, title)

        c.setStrokeColor(PDF_LINE)
        c.setLineWidth(1)
        c.line(x + 12, y + h - 28, x + w - 12, y + h - 28)


def _kv(c: canvas.Canvas, x, y, label, value, good_bad=None):
    c.setFont("Helvetica", 9)
    c.setFillColor(PDF_MUTED)
    c.drawString(x, y, label)

    c.setFont("Helvetica-Bold", 11)
    if good_bad == "good":
        c.setFillColor(PDF_GREEN)
    elif good_bad == "bad":
        c.setFillColor(PDF_RED)
    else:
        c.setFillColor(PDF_TEXT)

    c.drawRightString(x + 240, y, value)


def _table(c: canvas.Canvas, x, y_top, col_widths, headers, rows, row_h=16):
    table_w = sum(col_widths)

    header_h = 22
    c.setFillColor(colors.HexColor("#111c35"))
    c.setStrokeColor(PDF_LINE)
    c.roundRect(x, y_top - header_h, table_w, header_h, 8, fill=1, stroke=1)

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(PDF_TEXT)
    cx = x
    for i, htxt in enumerate(headers):
        c.drawString(cx + 10, y_top - 15, str(htxt))
        cx += col_widths[i]

    y = y_top - header_h - 8
    c.setFont("Helvetica", 9)

    gap = 4  # <- menor pra caber mais linhas sem estourar
    for r_i, r in enumerate(rows):
        fill = colors.HexColor("#0e1730") if (r_i % 2 == 0) else colors.HexColor("#0c142a")
        c.setFillColor(fill)
        c.setStrokeColor(PDF_LINE)
        c.roundRect(x, y - row_h, table_w, row_h, 8, fill=1, stroke=1)

        cx = x
        c.setFillColor(PDF_TEXT)
        for i, cell in enumerate(r):
            txt = "" if cell is None else str(cell)
            c.drawString(cx + 10, y - 12, txt[:60])
            cx += col_widths[i]

        y -= (row_h + gap)

    return y


# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = (request.form.get("senha") or "").strip()

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.senha, senha):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Login inválido!", "error")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/admin/fechamento/<int:year>/<int:month>/<int:week_no>", methods=["GET", "POST"], endpoint="weekly_edit")
@login_required
@admin_required
def weekly_edit(year, month, week_no):
    cfg = get_or_create_month_config(year, month)
    fixed_total, _ = get_fixed_costs_sum(year, month)

    row = WeeklyClose.query.filter_by(year=year, month=month, week_no=week_no).first()

    if request.method == "POST":
        period_start = (request.form.get("period_start") or "").strip() or None
        period_end = (request.form.get("period_end") or "").strip() or None

        revenue = _to_float(request.form.get("revenue"), 0.0)
        fuel = _to_float(request.form.get("fuel"), 0.0)
        extra = _to_float(request.form.get("extra_expenses"), 0.0)

        cargo = _to_float(request.form.get("cargo_insurance_weekly"), 250.0)
        miles = _to_float(request.form.get("miles"), 0.0)
        gallons = _to_float(request.form.get("gallons"), 0.0)

        notes = (request.form.get("notes") or "").strip() or None
        payment_status = (request.form.get("payment_status") or "pendente").strip().lower()
        if payment_status not in ("pendente", "pago"):
            payment_status = "pendente"

        if not row:
            row = WeeklyClose(year=year, month=month, week_no=week_no)
            db.session.add(row)

        row.period_start = period_start
        row.period_end = period_end
        row.revenue = revenue
        row.fuel = fuel
        row.extra_expenses = extra
        row.cargo_insurance_weekly = cargo
        row.miles = miles
        row.gallons = gallons
        row.notes = notes
        row.payment_status = payment_status

        db.session.commit()
        flash("Fechamento da semana salvo.", "success")
        return redirect(url_for("dashboard", year=year, month=month))

    if not row:
        row = WeeklyClose(year=year, month=month, week_no=week_no, cargo_insurance_weekly=250.0)
    preview = compute_week_calc(row, cfg.weeks_in_month, fixed_total)

    return render_template(
        "weekly_close_form.html",
        year=year, month=month, week_no=week_no,
        weeks_in_month=cfg.weeks_in_month,
        fixed_month_total=fixed_total,
        row=row,
        preview=preview
    )


@app.route("/admin/fechamento/<int:year>/<int:month>/<int:week_no>/duplicate", methods=["POST", "GET"])
@login_required
@admin_required
def weekly_duplicate_from_prev(year, month, week_no):
    if week_no <= 1:
        flash("Não existe semana anterior para copiar.", "error")
        return redirect(url_for("weekly_edit", year=year, month=month, week_no=week_no))

    prev = WeeklyClose.query.filter_by(year=year, month=month, week_no=week_no - 1).first()
    if not prev:
        flash("Semana anterior não encontrada. Cadastre a semana anterior primeiro.", "error")
        return redirect(url_for("weekly_edit", year=year, month=month, week_no=week_no))

    row = WeeklyClose.query.filter_by(year=year, month=month, week_no=week_no).first()
    if not row:
        row = WeeklyClose(year=year, month=month, week_no=week_no)
        db.session.add(row)

    row.period_start = prev.period_start
    row.period_end = prev.period_end
    row.revenue = prev.revenue
    row.fuel = prev.fuel
    row.extra_expenses = prev.extra_expenses
    row.cargo_insurance_weekly = prev.cargo_insurance_weekly
    row.miles = prev.miles
    row.gallons = prev.gallons
    row.notes = prev.notes
    row.payment_status = "pendente"

    db.session.commit()
    flash(f"Copiado da semana {week_no-1} para a semana {week_no}.", "success")
    return redirect(url_for("weekly_edit", year=year, month=month, week_no=week_no))


# =========================
# PDF PREMIUM ROUTES
# =========================
@app.route("/admin/fechamento/<int:year>/<int:month>/<int:week_no>/pdf")
@login_required
@admin_required
def weekly_pdf(year, month, week_no):
    cfg = get_or_create_month_config(year, month)
    fixed_total, _ = get_fixed_costs_sum(year, month)

    wrow = WeeklyClose.query.filter_by(year=year, month=month, week_no=week_no).first()
    if not wrow:
        flash("Semana não encontrada para exportar PDF.", "error")
        return redirect(url_for("dashboard", year=year, month=month))

    calc = compute_week_calc(wrow, cfg.weeks_in_month, fixed_total)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    page = 1
    meta_left = f"Year/Month: {year}/{month:02d} • Week: {week_no}"
    meta_right = f"Status: {(wrow.payment_status or 'pendente').upper()}"
    y = _draw_header(c, PDF_BRAND, PDF_SUBTITLE_WEEK, meta_left, meta_right)

    x = 36
    w_panel = letter[0] - 72
    _panel(c, x, y - 170, w_panel, 160, title="Summary")

    left = x + 14
    top = y - 50

    net = float(calc.get("net") or 0.0)
    net_flag = "good" if net >= 0 else "bad"

    _kv(c, left, top, "Revenue", _fmt_money(calc.get("revenue")), None)
    _kv(c, left, top - 22, "Fuel", _fmt_money(calc.get("fuel")), None)
    _kv(c, left, top - 44, "Extra Expenses", _fmt_money(calc.get("extra")), None)
    _kv(c, left, top - 66, "Cargo Insurance", _fmt_money(calc.get("cargo")), None)

    right = x + w_panel - 14 - 240
    _kv(c, right, top, "Driver (30%)", _fmt_money(calc.get("driver")), None)
    _kv(c, right, top - 22, "Dispatcher (12%)", _fmt_money(calc.get("dispatcher")), None)
    _kv(c, right, top - 44, "Fixed (rated weekly)", _fmt_money(calc.get("fixed_week")), None)
    _kv(c, right, top - 66, "Total Expenses", _fmt_money(calc.get("total_expenses")), None)

    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 14, y - 170 + 18, "Net (Profit)")

    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(PDF_GREEN if net >= 0 else PDF_RED)
    c.drawRightString(x + w_panel - 14, y - 170 + 14, _fmt_money(net))

    y2 = (y - 170) - 18
    _panel(c, x, y2 - 92, w_panel, 86, title="Details")

    mpg = calc.get("mpg")
    mpg_txt = f"{float(mpg):.2f}" if mpg is not None else "--"

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 14, y2 - 48, "Period")
    c.setFillColor(PDF_TEXT)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 14, y2 - 66, f"{wrow.period_start or '--'}  →  {wrow.period_end or '--'}")

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawRightString(x + w_panel - 14, y2 - 48, "MPG")
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(PDF_TEXT)
    c.drawRightString(x + w_panel - 14, y2 - 66, mpg_txt)

    notes = _safe_text(wrow.notes)
    y3 = (y2 - 92) - 18
    if notes:
        _panel(c, x, y3 - 150, w_panel, 144, title="Notes")
        c.setFont("Helvetica", 10)
        c.setFillColor(PDF_TEXT)

        max_w = w_panel - 28
        words = notes.split()
        line = ""
        yy = y3 - 50
        for wds in words:
            test = (line + " " + wds).strip()
            if stringWidth(test, "Helvetica", 10) <= max_w:
                line = test
            else:
                c.drawString(x + 14, yy, line)
                yy -= 14
                line = wds
                if yy < 80:
                    _draw_footer(c, page)
                    c.showPage()
                    page += 1
                    y = _draw_header(c, PDF_BRAND, PDF_SUBTITLE_WEEK, meta_left, meta_right)
                    _panel(c, x, y - 150, w_panel, 144, title="Notes (cont.)")
                    yy = y - 50
        if line:
            c.drawString(x + 14, yy, line)

    _draw_footer(c, page)
    c.showPage()
    c.save()

    buffer.seek(0)
    filename = f"ironway_week_{year}_{month:02d}_W{week_no}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")


@app.route("/admin/mes/<int:year>/<int:month>/pdf")
@login_required
@admin_required
def monthly_pdf(year, month):
    cfg, fixed_month_total, weeks_rows, week_cards, totals, mpg_month = compute_month_aggregate(year, month)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    page = 1
    meta_left = f"Year/Month: {year}/{month:02d} • Rateio: {cfg.weeks_in_month} weeks"
    meta_right = f"Fixed (month): {_fmt_money(fixed_month_total)}"

    # ==== tabela (ajustada pra caber dentro do panel)
    headers = ["Week", "Period", "Revenue", "Expenses", "Net", "MPG", "Status"]
    col_widths = [38, 160, 78, 78, 72, 46, 40]  # total = 512 (cabe certinho)
    row_h = 16

    # monta rows
    rows = []
    for wc in week_cards:
        mpg = wc["calc"]["mpg"]
        mpg_txt = f"{float(mpg):.2f}" if mpg is not None else "--"
        status = (wc.get("payment_status") or "pendente").upper()
        rows.append([
            str(wc["week_no"]),
            (wc["period"] or "")[:30],
            _fmt_money(wc["calc"]["revenue"]),
            _fmt_money(wc["calc"]["total_expenses"]),
            _fmt_money(wc["calc"]["net"]),
            mpg_txt,
            status
        ])

    def draw_page_header():
        nonlocal page
        y = _draw_header(c, PDF_BRAND, PDF_SUBTITLE_MONTH, meta_left, meta_right)

        x = 36
        w_panel = letter[0] - 72

        # ===== Summary panel
        _panel(c, x, y - 170, w_panel, 160, title="Monthly Summary")

        net = float(totals.get("net") or 0.0)
        net_flag = "good" if net >= 0 else "bad"

        left = x + 14
        top = y - 50

        _kv(c, left, top, "Revenue", _fmt_money(totals.get("revenue")), None)
        _kv(c, left, top - 22, "Total Expenses", _fmt_money(totals.get("expenses")), None)
        _kv(c, left, top - 44, "Net (Profit)", _fmt_money(net), net_flag)

        right = x + w_panel - 14 - 240
        _kv(c, right, top, "Fuel", _fmt_money(totals.get("fuel")), None)
        _kv(c, right, top - 22, "Driver (30%)", _fmt_money(totals.get("driver")), None)
        _kv(c, right, top - 44, "Dispatcher (12%)", _fmt_money(totals.get("dispatcher")), None)

        c.setFont("Helvetica", 10)
        c.setFillColor(PDF_MUTED)
        c.drawString(x + 14, y - 170 + 18, "MPG (month avg)")
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(PDF_TEXT)
        c.drawString(x + 14, y - 170 + 2, _fmt_num(mpg_month, 2) if mpg_month is not None else "--")

        # ===== Weeks panel “base”
        y2 = (y - 170) - 18
        _panel(c, x, y2 - 350, w_panel, 344, title=("Weeks Detail" if page == 1 else "Weeks Detail (cont.)"))

        # posição inicial da tabela
        table_x = x + 14
        table_top = y2 - 48

        return table_x, table_top

    # ===== primeira página
    table_x, table_top = draw_page_header()

    # ===== paginação real (pra nunca passar do rodapé)
    bottom_limit = 70  # guarda espaço pro footer
    header_h = 22
    gap = 4
    per_row = row_h + gap
    available = (table_top - header_h - 8) - bottom_limit
    rows_per_page = max(1, int(available // per_row))

    start = 0
    while start < len(rows):
        part = rows[start:start + rows_per_page]
        _table(c, table_x, table_top, col_widths, headers, part, row_h=row_h)
        start += rows_per_page

        if start < len(rows):
            _draw_footer(c, page)
            c.showPage()
            page += 1
            table_x, table_top = draw_page_header()

    _draw_footer(c, page)
    c.showPage()
    c.save()

    buffer.seek(0)
    filename = f"ironway_month_{year}_{month:02d}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")


@app.route("/dashboard")
@login_required
@admin_required
def dashboard():
    today = date.today()
    year = _to_int(request.args.get("year"), today.year)
    month = _to_int(request.args.get("month"), today.month)

    cfg = get_or_create_month_config(year, month)
    fixed_month_total, fixed_rows = get_fixed_costs_sum(year, month)

    weeks = WeeklyClose.query.filter_by(year=year, month=month).order_by(WeeklyClose.week_no.asc()).all()

    week_cards = []

    month_revenue = 0.0
    month_expenses = 0.0
    month_net = 0.0

    fuel_month = 0.0
    driver_month = 0.0
    dispatcher_month = 0.0
    fixed_rateated_month = 0.0

    mpg_sum = 0.0
    mpg_n = 0

    for w in weeks:
        calc = compute_week_calc(w, cfg.weeks_in_month, fixed_month_total)

        month_revenue += calc["revenue"]
        month_expenses += calc["total_expenses"]
        month_net += calc["net"]

        fuel_month += calc["fuel"]
        driver_month += calc["driver"]
        dispatcher_month += calc["dispatcher"]
        fixed_rateated_month += calc["fixed_week"]

        if calc["mpg"] is not None:
            mpg_sum += float(calc["mpg"])
            mpg_n += 1

        week_cards.append({
            "id": w.id,
            "week_no": int(w.week_no),
            "label": f"Sem {w.week_no}",
            "period": f"{w.period_start or '--'} → {w.period_end or '--'}",
            "payment_status": w.payment_status,
            "calc": calc,
        })

    monthly = {"revenue": month_revenue, "expenses": month_expenses, "net": month_net}
    mpg_month = (mpg_sum / mpg_n) if mpg_n > 0 else None

    weeks_labels = [w["label"] for w in week_cards] or ["Sem 1", "Sem 2", "Sem 3", "Sem 4"]
    weeks_revenue = [round(w["calc"]["revenue"], 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_expenses = [round(w["calc"]["total_expenses"], 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_net = [round(w["calc"]["net"], 2) for w in week_cards] or [0, 0, 0, 0]

    week_x = _to_int(request.args.get("week_x"), 1)

    months_list = []
    y, m = year, month
    for _ in range(6):
        months_list.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months_list = list(reversed(months_list))

    across_labels, across_revenue, across_expenses, across_net = [], [], [], []

    for (yy, mm) in months_list:
        cfg2 = get_or_create_month_config(yy, mm)
        fixed_total2, _ = get_fixed_costs_sum(yy, mm)
        w = WeeklyClose.query.filter_by(year=yy, month=mm, week_no=week_x).first()

        across_labels.append(month_label(yy, mm))
        if not w:
            across_revenue.append(0)
            across_expenses.append(0)
            across_net.append(0)
        else:
            c2 = compute_week_calc(w, cfg2.weeks_in_month, fixed_total2)
            across_revenue.append(round(c2["revenue"], 2))
            across_expenses.append(round(c2["total_expenses"], 2))
            across_net.append(round(c2["net"], 2))

    months12 = []
    y3, m3 = year, month
    for _ in range(12):
        months12.append((y3, m3))
        m3 -= 1
        if m3 == 0:
            m3 = 12
            y3 -= 1
    months12 = list(reversed(months12))

    chart_month_labels = []
    chart_month_net = []
    for (yy, mm) in months12:
        _, _, _, _, totals2, _ = compute_month_aggregate(yy, mm)
        chart_month_labels.append(month_label(yy, mm))
        chart_month_net.append(round(totals2["net"], 2))

    return render_template(
        "admin_dashboard.html",
        year=year,
        month=month,
        week_x=week_x,

        monthly=monthly,
        weeks=week_cards,

        weeks_in_month=cfg.weeks_in_month,
        fixed_month_total=fixed_month_total,
        fixed_rows=fixed_rows,

        fuel_month=fuel_month,
        driver_month=driver_month,
        dispatcher_month=dispatcher_month,
        fixed_rateated_month=fixed_rateated_month,

        mpg_month=mpg_month,

        chart_weeks_labels=weeks_labels,
        chart_weeks_revenue=weeks_revenue,
        chart_weeks_expenses=weeks_expenses,
        chart_weeks_net=weeks_net,

        chart_across_labels=across_labels,
        chart_across_revenue=across_revenue,
        chart_across_expenses=across_expenses,
        chart_across_net=across_net,

        chart_month_labels=chart_month_labels,
        chart_month_net=chart_month_net,

        receipt={"ok": False},
    )


@app.route("/admin/historico")
@login_required
@admin_required
def admin_history():
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(18):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    rows = []
    for (yy, mm) in months:
        cfg, fixed_month_total, weeks_rows, week_cards, totals, mpg_month = compute_month_aggregate(yy, mm)
        rows.append({
            "year": yy,
            "month": mm,
            "label": f"{yy}-{mm:02d}",
            "weeks_in_month": cfg.weeks_in_month,
            "fixed_month_total": fixed_month_total,
            "revenue": totals["revenue"],
            "expenses": totals["expenses"],
            "net": totals["net"],
            "mpg_month": mpg_month,
            "weeks_count": len(weeks_rows),
        })

    return render_template("admin_history.html", rows=rows)


@app.route("/admin/fixos", methods=["GET", "POST"])
@login_required
@admin_required
def admin_fixos():
    today = date.today()
    year = _to_int(request.args.get("year"), today.year)
    month = _to_int(request.args.get("month"), today.month)

    cfg = get_or_create_month_config(year, month)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "save_weeks":
            weeks_in_month = _to_int(request.form.get("weeks_in_month"), 4)
            if weeks_in_month not in (4, 5):
                weeks_in_month = 4
            cfg.weeks_in_month = weeks_in_month
            db.session.commit()
            flash("Config do mês salva (4/5 semanas).", "success")
            return redirect(url_for("admin_fixos", year=year, month=month))

        if action == "add_cost":
            name = (request.form.get("name") or "").strip()
            amount = _to_float(request.form.get("amount_monthly"), 0.0)
            if not name:
                flash("Informe o nome do custo.", "error")
                return redirect(url_for("admin_fixos", year=year, month=month))

            row = MonthlyFixedCost(year=year, month=month, name=name, amount_monthly=amount)
            db.session.add(row)
            db.session.commit()
            flash("Custo mensal adicionado.", "success")
            return redirect(url_for("admin_fixos", year=year, month=month))

        if action == "delete_cost":
            cid = _to_int(request.form.get("cost_id"), 0)
            row = MonthlyFixedCost.query.get(cid)
            if row:
                db.session.delete(row)
                db.session.commit()
                flash("Custo removido.", "success")
            return redirect(url_for("admin_fixos", year=year, month=month))

    fixed_total, fixed_rows = get_fixed_costs_sum(year, month)

    return render_template(
        "monthly_fixed_costs.html",
        year=year, month=month,
        weeks_in_month=cfg.weeks_in_month,
        fixed_total=fixed_total,
        fixed_rows=fixed_rows
    )


# =========================
# INIT
# =========================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()

        if not User.query.filter_by(email="admin@sistema.com").first():
            admin = User(
                nome="Administrador",
                email="admin@sistema.com",
                senha=generate_password_hash("123456"),
                tipo="admin",
            )
            db.session.add(admin)
            db.session.commit()

    print(">>> INICIANDO FLASK AGORA...")
    app.run(debug=True, host="127.0.0.1", port=5000, use_reloader=False)