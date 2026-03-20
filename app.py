import os
import io
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin,
    login_user, login_required, logout_user,
    current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text

# PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth

import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib.utils import ImageReader

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
    tipo = db.Column(db.String(40), nullable=False, default="admin")

    def __repr__(self):
        return f"<User {self.email}>"


class MonthlyConfig(db.Model):
    """
    Config do mês:
    - semanas do mês
    - % motorista
    - % dispatcher
    - meta do preço por galão
    """
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)

    weeks_in_month = db.Column(db.Integer, nullable=False, default=4)

    driver_percent = db.Column(db.Float, nullable=False, default=30.0)
    dispatcher_percent = db.Column(db.Float, nullable=False, default=10.0)
    fuel_target_price = db.Column(db.Float, nullable=False, default=3.30)

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
    Fechamento da semana.
    """
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)

    week_no = db.Column(db.Integer, nullable=False, index=True)

    period_start = db.Column(db.String(20), nullable=True)
    period_end = db.Column(db.String(20), nullable=True)

    revenue = db.Column(db.Float, nullable=False, default=0.0)

    fuel = db.Column(db.Float, nullable=False, default=0.0)
    extra_expenses = db.Column(db.Float, nullable=False, default=0.0)
    cargo_insurance_weekly = db.Column(db.Float, nullable=False, default=250.0)

    miles = db.Column(db.Float, nullable=False, default=0.0)
    gallons = db.Column(db.Float, nullable=False, default=0.0)

    notes = db.Column(db.Text, nullable=True)
    payment_status = db.Column(db.String(20), nullable=False, default="pendente")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    motorista_id = db.Column(db.Integer, db.ForeignKey("motoristas.id"), nullable=True)
    dispatcher_id = db.Column(db.Integer, db.ForeignKey("dispatchers.id"), nullable=True)
    veiculo_id = db.Column(db.Integer, db.ForeignKey("veiculos.id"), nullable=True)

    motorista = db.relationship("Motorista", foreign_keys=[motorista_id])
    dispatcher = db.relationship("Dispatcher", foreign_keys=[dispatcher_id])
    veiculo = db.relationship("Veiculo", foreign_keys=[veiculo_id])

    __table_args__ = (
        db.UniqueConstraint("year", "month", "week_no", name="uq_weekclose_year_month_week"),
    )


class Motorista(db.Model):
    __tablename__ = "motoristas"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    telefone = db.Column(db.String(40), nullable=True)
    observacoes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Dispatcher(db.Model):
    __tablename__ = "dispatchers"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    telefone = db.Column(db.String(40), nullable=True)
    observacoes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Veiculo(db.Model):
    __tablename__ = "veiculos"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)       # ex: Amarok / Hilux / Caminhão Volvo
    placa = db.Column(db.String(30), nullable=True)
    modelo = db.Column(db.String(120), nullable=True)
    observacoes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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


def _money(v):
    try:
        return f"${float(v or 0):,.2f}"
    except Exception:
        return "$0.00"


def _safe_text(s):
    return (s or "").strip()


def _pct(value):
    try:
        return f"{float(value or 0):.2f}%"
    except Exception:
        return "0.00%"


def ensure_schema_updates():
    """
    Faz upgrade leve do SQLite sem migration tool.
    Adiciona colunas novas se ainda não existirem.
    """
    inspector = inspect(db.engine)

    if "monthly_config" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("monthly_config")}

        stmts = []
        if "driver_percent" not in cols:
            stmts.append("ALTER TABLE monthly_config ADD COLUMN driver_percent FLOAT DEFAULT 30.0")
        if "dispatcher_percent" not in cols:
            stmts.append("ALTER TABLE monthly_config ADD COLUMN dispatcher_percent FLOAT DEFAULT 10.0")
        if "fuel_target_price" not in cols:
            stmts.append("ALTER TABLE monthly_config ADD COLUMN fuel_target_price FLOAT DEFAULT 3.30")

        for stmt in stmts:
            db.session.execute(text(stmt))
        if stmts:
            db.session.commit()

    if "weekly_close" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("weekly_close")}

        stmts = []
        if "motorista_id" not in cols:
            stmts.append("ALTER TABLE weekly_close ADD COLUMN motorista_id INTEGER")
        if "dispatcher_id" not in cols:
            stmts.append("ALTER TABLE weekly_close ADD COLUMN dispatcher_id INTEGER")
        if "veiculo_id" not in cols:
            stmts.append("ALTER TABLE weekly_close ADD COLUMN veiculo_id INTEGER")

        for stmt in stmts:
            db.session.execute(text(stmt))
        if stmts:
            db.session.commit()


def get_or_create_month_config(year, month):
    cfg = MonthlyConfig.query.filter_by(year=year, month=month).first()
    if not cfg:
        cfg = MonthlyConfig(
            year=year,
            month=month,
            weeks_in_month=4,
            driver_percent=30.0,
            dispatcher_percent=10.0,
            fuel_target_price=3.30,
        )
        db.session.add(cfg)
        db.session.commit()

    if cfg.driver_percent is None:
        cfg.driver_percent = 30.0
    if cfg.dispatcher_percent is None:
        cfg.dispatcher_percent = 10.0
    if cfg.fuel_target_price is None:
        cfg.fuel_target_price = 3.30
    db.session.commit()

    return cfg


def get_fixed_costs_sum(year, month):
    rows = MonthlyFixedCost.query.filter_by(year=year, month=month).all()
    total = float(sum((r.amount_monthly or 0) for r in rows))
    return total, rows


def compute_week_calc(w: WeeklyClose, weeks_in_month: int, fixed_month_total: float, cfg: MonthlyConfig | None = None):
    revenue = float(w.revenue or 0)
    fuel = float(w.fuel or 0)
    extra = float(w.extra_expenses or 0)
    cargo = float(w.cargo_insurance_weekly or 0)
    miles = float(w.miles or 0)
    gallons = float(w.gallons or 0)

    driver_percent = float(getattr(cfg, "driver_percent", 30.0) or 30.0)
    dispatcher_percent = float(getattr(cfg, "dispatcher_percent", 10.0) or 10.0)
    fuel_target_price = float(getattr(cfg, "fuel_target_price", 3.30) or 3.30)

    driver = revenue * (driver_percent / 100.0)
    dispatcher = revenue * (dispatcher_percent / 100.0)

    weeks_in_month = 4 if weeks_in_month not in (4, 5) else weeks_in_month
    fixed_week = fixed_month_total / weeks_in_month if weeks_in_month > 0 else 0.0

    total_expenses = fuel + extra + cargo + driver + dispatcher + fixed_week
    net = revenue - total_expenses

    dollars_per_mile = (revenue / miles) if miles > 0 else None
    avg_fuel_price = (fuel / gallons) if gallons > 0 else None
    result_percent = ((net / revenue) * 100.0) if revenue > 0 else 0.0

    fuel_vs_target_percent = None
    if avg_fuel_price is not None and fuel_target_price > 0:
        fuel_vs_target_percent = ((avg_fuel_price - fuel_target_price) / fuel_target_price) * 100.0

    return {
        "revenue": revenue,
        "fuel": fuel,
        "extra": extra,
        "cargo": cargo,
        "driver": driver,
        "dispatcher": dispatcher,
        "driver_percent": driver_percent,
        "dispatcher_percent": dispatcher_percent,
        "fixed_week": fixed_week,
        "total_expenses": total_expenses,
        "net": net,

        # novos indicadores
        "dollars_per_mile": dollars_per_mile,
        "avg_fuel_price": avg_fuel_price,
        "fuel_target_price": fuel_target_price,
        "fuel_vs_target_percent": fuel_vs_target_percent,
        "result_percent": result_percent,

        # compatibilidade temporária com templates antigos
        "mpg": dollars_per_mile,
    }


def compute_month_aggregate(year: int, month: int):
    cfg = get_or_create_month_config(year, month)
    fixed_month_total, _ = get_fixed_costs_sum(year, month)

    motoristas = Motorista.query.order_by(Motorista.nome.asc()).all()
    dispatchers = Dispatcher.query.order_by(Dispatcher.nome.asc()).all()
    veiculos = Veiculo.query.order_by(Veiculo.nome.asc()).all()

    weeks_rows = WeeklyClose.query.filter_by(year=year, month=month)\
        .order_by(WeeklyClose.week_no.asc()).all()

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
        "miles": 0.0,
        "gallons": 0.0,
    }

    dpm_sum = 0.0
    dpm_n = 0

    fuel_avg_sum = 0.0
    fuel_avg_n = 0

    result_pct_sum = 0.0
    result_pct_n = 0

    week_cards = []

    for w in weeks_rows:
        calc = compute_week_calc(w, cfg.weeks_in_month, fixed_month_total, cfg)

        # 🔥 GARANTIR FUEL SEMPRE
        fuel_value = float(calc.get("fuel") or 0)

        totals["revenue"] += float(calc.get("revenue") or 0)
        totals["expenses"] += float(calc.get("total_expenses") or 0)
        totals["net"] += float(calc.get("net") or 0)
        totals["fuel"] += fuel_value
        totals["extra"] += float(calc.get("extra") or 0)
        totals["cargo"] += float(calc.get("cargo") or 0)
        totals["driver"] += float(calc.get("driver") or 0)
        totals["dispatcher"] += float(calc.get("dispatcher") or 0)
        totals["fixed_week_total"] += float(calc.get("fixed_week") or 0)

        totals["miles"] += float(w.miles or 0)
        totals["gallons"] += float(w.gallons or 0)

        if calc.get("dollars_per_mile") is not None:
            dpm_sum += float(calc["dollars_per_mile"])
            dpm_n += 1

        if calc.get("avg_fuel_price") is not None:
            fuel_avg_sum += float(calc["avg_fuel_price"])
            fuel_avg_n += 1

        result_pct_sum += float(calc.get("result_percent") or 0)
        result_pct_n += 1

        week_cards.append({
            "week_no": int(w.week_no),
            "label": f"Sem {w.week_no}",
            "period": f"{w.period_start or '--'} → {w.period_end or '--'}",
            "payment_status": w.payment_status,
            "calc": {
                **calc,
                "fuel": fuel_value  # 🔥 garante que sempre tem fuel
            },
        })

    dollars_per_mile_month = (dpm_sum / dpm_n) if dpm_n > 0 else None
    avg_fuel_price_month = (fuel_avg_sum / fuel_avg_n) if fuel_avg_n > 0 else None
    result_percent_month = (result_pct_sum / result_pct_n) if result_pct_n > 0 else 0.0

    return (
        cfg,
        fixed_month_total,
        weeks_rows,
        week_cards,
        totals,
        dollars_per_mile_month,
        avg_fuel_price_month,
        result_percent_month,
    )


def _build_chart_image(chart_type, labels, datasets, title="", height=2.8):
    fig, ax = plt.subplots(figsize=(8.8, height), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if chart_type == "bar":
        n = max(1, len(datasets))
        x = list(range(len(labels)))
        width = 0.36 if n == 2 else 0.22

        if n == 1:
            ds = datasets[0]
            ax.bar(x, ds["data"], width=0.55, label=ds["label"])
        else:
            offsets = []
            if n == 2:
                offsets = [-width / 2, width / 2]
            elif n == 3:
                offsets = [-width, 0, width]
            else:
                offsets = [0] * n

            for i, ds in enumerate(datasets):
                xpos = [v + offsets[i] for v in x]
                ax.bar(xpos, ds["data"], width=width, label=ds["label"])

        ax.set_xticks(list(range(len(labels))))
        ax.set_xticklabels(labels, rotation=0)

    elif chart_type == "line":
        for ds in datasets:
            ax.plot(labels, ds["data"], marker="o", linewidth=2.2, label=ds["label"])

    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.grid(axis="y", linestyle="--", alpha=0.25)
    ax.tick_params(axis="x", labelsize=8)
    ax.tick_params(axis="y", labelsize=8)

    if len(datasets) > 1:
        ax.legend(fontsize=8, frameon=False, loc="best")

    plt.tight_layout()

    img_buffer = io.BytesIO()
    fig.savefig(img_buffer, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    img_buffer.seek(0)
    return img_buffer


def _draw_chart_from_buffer(c, img_buffer, x, y, w, h):
    img = ImageReader(img_buffer)
    c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")


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
PDF_YELLOW = colors.HexColor("#facc15")


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
    elif good_bad == "warn":
        c.setFillColor(PDF_YELLOW)
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

    gap = 4
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

    motoristas = Motorista.query.order_by(Motorista.nome.asc()).all()
    dispatchers = Dispatcher.query.order_by(Dispatcher.nome.asc()).all()
    veiculos = Veiculo.query.order_by(Veiculo.nome.asc()).all()

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

        motorista_id = _to_int(request.form.get("motorista_id"), 0) or None
        dispatcher_id = _to_int(request.form.get("dispatcher_id"), 0) or None
        veiculo_id = _to_int(request.form.get("veiculo_id"), 0) or None

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
        row.motorista_id = motorista_id
        row.dispatcher_id = dispatcher_id
        row.veiculo_id = veiculo_id
        row.notes = notes
        row.payment_status = payment_status

        db.session.commit()
        flash("Fechamento da semana salvo.", "success")
        return redirect(url_for("dashboard", year=year, month=month))

    if not row:
        row = WeeklyClose(
            year=year,
            month=month,
            week_no=week_no,
            cargo_insurance_weekly=250.0
        )

    preview = compute_week_calc(row, cfg.weeks_in_month, fixed_total, cfg)

    return render_template(
        "weekly_close_form.html",
        year=year,
        month=month,
        week_no=week_no,
        weeks_in_month=cfg.weeks_in_month,
        fixed_month_total=fixed_total,
        row=row,
        preview=preview,
        driver_percent=cfg.driver_percent,
        dispatcher_percent=cfg.dispatcher_percent,
        fuel_target_price=cfg.fuel_target_price,
        motoristas=motoristas,
        dispatchers=dispatchers,
        veiculos=veiculos,
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
# PDF ROUTES
# =========================
@app.route("/admin/fechamento/<int:year>/<int:month>/<int:week_no>/pdf")
@login_required
@admin_required
def weekly_pdf(year, month, week_no):
    cfg = get_or_create_month_config(year, month)
    fixed_total, _ = get_fixed_costs_sum(year, month)

    from sqlalchemy.orm import joinedload

    wrow = (
        WeeklyClose.query
        .options(
            joinedload(WeeklyClose.motorista),
            joinedload(WeeklyClose.dispatcher),
            joinedload(WeeklyClose.veiculo),
        )
        .filter_by(year=year, month=month, week_no=week_no)
        .first()
    )
    if not wrow:
        flash("Semana não encontrada para exportar PDF.", "error")
        return redirect(url_for("dashboard", year=year, month=month))

    calc = compute_week_calc(wrow, cfg.weeks_in_month, fixed_total, cfg)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    page = 1
    meta_left = f"Year/Month: {year}/{month:02d} • Week: {week_no}"
    meta_right = f"Status: {(wrow.payment_status or 'pendente').upper()}"
    y = _draw_header(c, PDF_BRAND, PDF_SUBTITLE_WEEK, meta_left, meta_right)

    x = 36
    w_panel = letter[0] - 72
    _panel(c, x, y - 200, w_panel, 190, title="Summary")

    left = x + 14
    top = y - 50

    net = float(calc.get("net") or 0.0)
    net_flag = "good" if net >= 0 else "bad"

    _kv(c, left, top, "Revenue", _fmt_money(calc.get("revenue")), None)
    _kv(c, left, top - 22, "Fuel", _fmt_money(calc.get("fuel")), None)
    _kv(c, left, top - 44, "Extra Expenses", _fmt_money(calc.get("extra")), None)
    _kv(c, left, top - 66, "Cargo Insurance", _fmt_money(calc.get("cargo")), None)
    _kv(c, left, top - 88, "Average Fuel Price", _fmt_money(calc.get("avg_fuel_price")), "warn")
    _kv(c, left, top - 110, "Fuel Target", _fmt_money(calc.get("fuel_target_price")), None)

    right = x + w_panel - 14 - 240
    _kv(c, right, top, f"Driver ({_pct(calc.get('driver_percent'))})", _fmt_money(calc.get("driver")), None)
    _kv(c, right, top - 22, f"Dispatcher ({_pct(calc.get('dispatcher_percent'))})", _fmt_money(calc.get("dispatcher")), None)
    _kv(c, right, top - 44, "Fixed (rated weekly)", _fmt_money(calc.get("fixed_week")), None)
    _kv(c, right, top - 66, "Total Expenses", _fmt_money(calc.get("total_expenses")), None)
    _kv(c, right, top - 88, "$ / Mile", _fmt_num(calc.get("dollars_per_mile"), 2), "good")
    _kv(c, right, top - 110, "Result %", _pct(calc.get("result_percent")), "good" if (calc.get("result_percent") or 0) >= 0 else "bad")

    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 14, y - 200 + 18, "Net (Profit)")

    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(PDF_GREEN if net >= 0 else PDF_RED)
    c.drawRightString(x + w_panel - 14, y - 200 + 14, _fmt_money(net))

    motorista_nome = "--"
    if getattr(wrow, "motorista", None):
        motorista_nome = getattr(wrow.motorista, "nome", "--") or "--"

    dispatcher_nome = "--"
    if getattr(wrow, "dispatcher", None):
        dispatcher_nome = getattr(wrow.dispatcher, "nome", "--") or "--"

    veiculo_nome = "--"
    if getattr(wrow, "veiculo", None):
        vnome = getattr(wrow.veiculo, "nome", None)
        vplaca = getattr(wrow.veiculo, "placa", None)
        if vnome and vplaca:
            veiculo_nome = f"{vnome} - {vplaca}"
        else:
            veiculo_nome = vnome or vplaca or "--"

    y2 = (y - 200) - 18
    _panel(c, x, y2 - 150, w_panel, 144, title="Details")

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 14, y2 - 42, "Period")
    c.setFillColor(PDF_TEXT)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 14, y2 - 58, f"{wrow.period_start or '--'}  →  {wrow.period_end or '--'}")

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 14, y2 - 82, "Motorista")
    c.setFillColor(PDF_TEXT)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 14, y2 - 98, motorista_nome)

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 220, y2 - 82, "Dispatcher")
    c.setFillColor(PDF_TEXT)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 220, y2 - 98, dispatcher_nome)

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawString(x + 14, y2 - 122, "Veículo / Frota")
    c.setFillColor(PDF_TEXT)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 14, y2 - 138, veiculo_nome)

    c.setFont("Helvetica", 10)
    c.setFillColor(PDF_MUTED)
    c.drawRightString(x + w_panel - 14, y2 - 42, "Miles / Gallons")
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(PDF_TEXT)
    c.drawRightString(x + w_panel - 14, y2 - 58, f"{_fmt_num(wrow.miles, 2)} / {_fmt_num(wrow.gallons, 2)}")

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


def _build_chart_image(chart_type, labels, datasets, title="", height=2.8):
    fig, ax = plt.subplots(figsize=(8.8, height), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if chart_type == "bar":
        n = max(1, len(datasets))
        x = list(range(len(labels)))
        width = 0.36 if n == 2 else 0.22

        if n == 1:
            ds = datasets[0]
            ax.bar(x, ds["data"], width=0.55, label=ds["label"])
        else:
            offsets = []
            if n == 2:
                offsets = [-width / 2, width / 2]
            elif n == 3:
                offsets = [-width, 0, width]
            else:
                offsets = [0] * n

            for i, ds in enumerate(datasets):
                xpos = [v + offsets[i] for v in x]
                ax.bar(xpos, ds["data"], width=width, label=ds["label"])

        ax.set_xticks(list(range(len(labels))))
        ax.set_xticklabels(labels, rotation=0)

    elif chart_type == "line":
        for ds in datasets:
            ax.plot(labels, ds["data"], marker="o", linewidth=2.2, label=ds["label"])

    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.grid(axis="y", linestyle="--", alpha=0.25)
    ax.tick_params(axis="x", labelsize=8)
    ax.tick_params(axis="y", labelsize=8)

    if len(datasets) > 1:
        ax.legend(fontsize=8, frameon=False, loc="best")

    plt.tight_layout()

    img_buffer = io.BytesIO()
    fig.savefig(img_buffer, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    img_buffer.seek(0)
    return img_buffer


def _draw_chart_from_buffer(c, img_buffer, x, y, w, h):
    img = ImageReader(img_buffer)
    c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")


@app.route("/admin/mes/<int:year>/<int:month>/pdf")
@login_required
@admin_required
def monthly_pdf(year, month):
    (
        cfg,
        fixed_month_total,
        weeks_rows,
        week_cards,
        totals,
        dollars_per_mile_month,
        avg_fuel_price_month,
        result_percent_month,
    ) = compute_month_aggregate(year, month)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    page = 1
    meta_left = f"Year/Month: {year}/{month:02d} • Rateio: {cfg.weeks_in_month} weeks"
    meta_right = f"Fixed (month): {_fmt_money(fixed_month_total)}"

    headers = ["Week", "Period", "Revenue", "Expenses", "Net", "$/Mile", "Fuel Avg"]
    col_widths = [38, 150, 74, 74, 72, 50, 54]
    row_h = 16

    rows = []
    for wc in week_cards:
        rows.append([
            str(wc["week_no"]),
            (wc["period"] or "")[:30],
            _fmt_money(wc["calc"]["revenue"]),
            _fmt_money(wc["calc"]["total_expenses"]),
            _fmt_money(wc["calc"]["net"]),
            _fmt_num(wc["calc"]["dollars_per_mile"], 2),
            _fmt_money(wc["calc"]["avg_fuel_price"]),
        ])

    def draw_page_header():
        nonlocal page
        y = _draw_header(c, PDF_BRAND, PDF_SUBTITLE_MONTH, meta_left, meta_right)

        x = 36
        w_panel = letter[0] - 72

        _panel(c, x, y - 190, w_panel, 180, title="Monthly Summary")

        net = float(totals.get("net") or 0.0)
        net_flag = "good" if net >= 0 else "bad"

        left = x + 14
        top = y - 50

        _kv(c, left, top, "Revenue", _fmt_money(totals.get("revenue")), None)
        _kv(c, left, top - 22, "Total Expenses", _fmt_money(totals.get("expenses")), None)
        _kv(c, left, top - 44, "Net (Profit)", _fmt_money(net), net_flag)
        _kv(c, left, top - 66, "Average Fuel Price", _fmt_money(avg_fuel_price_month), "warn")
        _kv(c, left, top - 88, "Fuel Target", _fmt_money(cfg.fuel_target_price), None)

        right = x + w_panel - 14 - 240
        _kv(c, right, top, f"Driver ({_pct(cfg.driver_percent)})", _fmt_money(totals.get("driver")), None)
        _kv(c, right, top - 22, f"Dispatcher ({_pct(cfg.dispatcher_percent)})", _fmt_money(totals.get("dispatcher")), None)
        _kv(c, right, top - 44, "$ / Mile (month avg)", _fmt_num(dollars_per_mile_month, 2), "good")
        _kv(c, right, top - 66, "Result % (month avg)", _pct(result_percent_month), "good" if result_percent_month >= 0 else "bad")
        _kv(c, right, top - 88, "Fuel", _fmt_money(totals.get("fuel")), None)

        y2 = (y - 190) - 18
        _panel(c, x, y2 - 350, w_panel, 344, title=("Weeks Detail" if page == 1 else "Weeks Detail (cont.)"))

        table_x = x + 14
        table_top = y2 - 48

        return table_x, table_top

    table_x, table_top = draw_page_header()

    bottom_limit = 70
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

    weeks_labels = [f"Sem {w['week_no']}" for w in week_cards] or ["Sem 1", "Sem 2", "Sem 3", "Sem 4"]
    weeks_revenue = [round(w["calc"]["revenue"] or 0, 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_expenses = [round(w["calc"]["total_expenses"] or 0, 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_net = [round(w["calc"]["net"] or 0, 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_avg_fuel = [round(w["calc"]["avg_fuel_price"], 2) if w["calc"]["avg_fuel_price"] is not None else 0 for w in week_cards] or [0, 0, 0, 0]
    weeks_fuel_target = [round(cfg.fuel_target_price or 0, 2) for _ in weeks_labels]

    chart_months = []
    chart_month_net = []

    y3, m3 = year, month
    months12 = []
    for _ in range(12):
        months12.append((y3, m3))
        m3 -= 1
        if m3 == 0:
            m3 = 12
            y3 -= 1
    months12 = list(reversed(months12))

    for (yy, mm) in months12:
        _, _, _, _, totals2, _, _, _ = compute_month_aggregate(yy, mm)
        chart_months.append(month_label(yy, mm))
        chart_month_net.append(round(totals2["net"] or 0, 2))

    _draw_footer(c, page)
    c.showPage()
    page += 1

    y = _draw_header(
        c,
        PDF_BRAND,
        "MONTHLY CHARTS",
        f"Year/Month: {year}/{month:02d}",
        "Charts Overview"
    )

    x = 36
    w_panel = letter[0] - 72

    chart1 = _build_chart_image(
        "bar",
        weeks_labels,
        [
            {"label": "Revenue", "data": weeks_revenue},
            {"label": "Expenses", "data": weeks_expenses},
        ],
        title="Revenue x Expenses by Week",
        height=2.6,
    )

    chart2 = _build_chart_image(
        "line",
        weeks_labels,
        [
            {"label": "Net", "data": weeks_net},
        ],
        title="Net by Week",
        height=2.6,
    )

    _panel(c, x, y - 250, w_panel, 235, title="Chart 1")
    _draw_chart_from_buffer(c, chart1, x + 12, y - 238, w_panel - 24, 205)

    y2 = (y - 250) - 18
    _panel(c, x, y2 - 250, w_panel, 235, title="Chart 2")
    _draw_chart_from_buffer(c, chart2, x + 12, y2 - 238, w_panel - 24, 205)

    _draw_footer(c, page)
    c.showPage()
    page += 1

    y = _draw_header(
        c,
        PDF_BRAND,
        "MONTHLY CHARTS",
        f"Year/Month: {year}/{month:02d}",
        "Fuel + Trend"
    )

    chart3 = _build_chart_image(
        "bar",
        weeks_labels,
        [
            {"label": "Fuel Avg", "data": weeks_avg_fuel},
            {"label": "Fuel Target", "data": weeks_fuel_target},
        ],
        title="Average Fuel Price x Target",
        height=2.6,
    )

    chart4 = _build_chart_image(
        "line",
        chart_months,
        [
            {"label": "Monthly Net", "data": chart_month_net},
        ],
        title="Monthly Net - Last 12 Months",
        height=2.6,
    )

    _panel(c, x, y - 250, w_panel, 235, title="Chart 3")
    _draw_chart_from_buffer(c, chart3, x + 12, y - 238, w_panel - 24, 205)

    y2 = (y - 250) - 18
    _panel(c, x, y2 - 250, w_panel, 235, title="Chart 4")
    _draw_chart_from_buffer(c, chart4, x + 12, y2 - 238, w_panel - 24, 205)

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

    from sqlalchemy.orm import joinedload

    weeks = (
        WeeklyClose.query
        .options(
            joinedload(WeeklyClose.motorista),
            joinedload(WeeklyClose.dispatcher),
            joinedload(WeeklyClose.veiculo),
        )
        .filter_by(year=year, month=month)
        .order_by(WeeklyClose.week_no.asc())
        .all()
    )

    motoristas_map = {m.id: m.nome for m in Motorista.query.all()}
    dispatchers_map = {d.id: d.nome for d in Dispatcher.query.all()}
    veiculos_map = {}
    for v in Veiculo.query.all():
        if v.placa and v.nome:
            veiculos_map[v.id] = f"{v.nome} - {v.placa}"
        else:
            veiculos_map[v.id] = v.nome or v.modelo or v.placa or "--"

    week_cards = []

    month_revenue = 0.0
    month_expenses = 0.0
    month_net = 0.0

    fuel_month = 0.0
    driver_month = 0.0
    dispatcher_month = 0.0
    fixed_rateated_month = 0.0

    dpm_sum = 0.0
    dpm_n = 0

    avg_fuel_sum = 0.0
    avg_fuel_n = 0

    result_pct_sum = 0.0
    result_pct_n = 0

    for w in weeks:
        calc = compute_week_calc(w, cfg.weeks_in_month, fixed_month_total, cfg)

        month_revenue += calc["revenue"]
        month_expenses += calc["total_expenses"]
        month_net += calc["net"]

        fuel_month += calc["fuel"]
        driver_month += calc["driver"]
        dispatcher_month += calc["dispatcher"]
        fixed_rateated_month += calc["fixed_week"]

        if calc["dollars_per_mile"] is not None:
            dpm_sum += float(calc["dollars_per_mile"])
            dpm_n += 1

        if calc["avg_fuel_price"] is not None:
            avg_fuel_sum += float(calc["avg_fuel_price"])
            avg_fuel_n += 1

        result_pct_sum += float(calc["result_percent"] or 0)
        result_pct_n += 1

        motorista_nome = motoristas_map.get(getattr(w, "motorista_id", None), "--")
        dispatcher_nome = dispatchers_map.get(getattr(w, "dispatcher_id", None), "--")
        veiculo_nome = veiculos_map.get(getattr(w, "veiculo_id", None), "--")

        week_cards.append({
            "id": w.id,
            "week_no": int(w.week_no),
            "label": f"Sem {w.week_no}",
            "period": f"{w.period_start or '--'} → {w.period_end or '--'}",
            "payment_status": w.payment_status,
            "motorista_nome": motorista_nome,
            "dispatcher_nome": dispatcher_nome,
            "veiculo_nome": veiculo_nome,
            "calc": calc,
        })

    monthly = {"revenue": month_revenue, "expenses": month_expenses, "net": month_net}
    dollars_per_mile_month = (dpm_sum / dpm_n) if dpm_n > 0 else None
    avg_fuel_price_month = (avg_fuel_sum / avg_fuel_n) if avg_fuel_n > 0 else None
    result_percent_month = (result_pct_sum / result_pct_n) if result_pct_n > 0 else 0.0

    weeks_labels = [w["label"] for w in week_cards] or ["Sem 1", "Sem 2", "Sem 3", "Sem 4"]
    weeks_revenue = [round(w["calc"]["revenue"], 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_expenses = [round(w["calc"]["total_expenses"], 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_net = [round(w["calc"]["net"], 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_dpm = [round(w["calc"]["dollars_per_mile"], 2) if w["calc"]["dollars_per_mile"] is not None else 0 for w in week_cards] or [0, 0, 0, 0]
    weeks_avg_fuel = [round(w["calc"]["avg_fuel_price"], 2) if w["calc"]["avg_fuel_price"] is not None else 0 for w in week_cards] or [0, 0, 0, 0]
    weeks_result_pct = [round(w["calc"]["result_percent"], 2) for w in week_cards] or [0, 0, 0, 0]
    weeks_fuel_target = [round(cfg.fuel_target_price, 2) for _ in weeks_labels] or [round(cfg.fuel_target_price, 2)] * 4
    chart_weeks_fuel = [round(w["calc"]["fuel"], 2) for w in week_cards] or [0, 0, 0, 0]

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
            c2 = compute_week_calc(w, cfg2.weeks_in_month, fixed_total2, cfg2)
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
        _, _, _, _, totals2, _, _, _ = compute_month_aggregate(yy, mm)
        chart_month_labels.append(month_label(yy, mm))
        chart_month_net.append(round(totals2["net"], 2))

    pie_labels = []
    pie_values = []

    if dispatcher_month > 0:
        pie_labels.append(f"Dispatcher ({cfg.dispatcher_percent:.2f}%)")
        pie_values.append(round(dispatcher_month, 2))

    if driver_month > 0:
        pie_labels.append(f"Driver ({cfg.driver_percent:.2f}%)")
        pie_values.append(round(driver_month, 2))

    if fuel_month > 0:
        pie_labels.append("Fuel")
        pie_values.append(round(fuel_month, 2))

    cargo_month = sum((w["calc"]["cargo"] or 0) for w in week_cards)
    if cargo_month > 0:
        pie_labels.append("Cargo Insurance")
        pie_values.append(round(cargo_month, 2))

    extra_month = sum((w["calc"]["extra"] or 0) for w in week_cards)
    if extra_month > 0:
        pie_labels.append("Extra Expenses")
        pie_values.append(round(extra_month, 2))

    if fixed_rateated_month > 0:
        pie_labels.append("Fixed Costs")
        pie_values.append(round(fixed_rateated_month, 2))

    current_week = week_cards[-1] if week_cards else None

    return render_template(
        "admin_dashboard.html",
        year=year,
        month=month,
        week_x=week_x,

        monthly=monthly,
        weeks=week_cards,
        current_week=current_week,

        weeks_in_month=cfg.weeks_in_month,
        fixed_month_total=fixed_month_total,
        fixed_rows=fixed_rows,

        fuel_month=fuel_month,
        driver_month=driver_month,
        dispatcher_month=dispatcher_month,
        fixed_rateated_month=fixed_rateated_month,

        driver_percent=cfg.driver_percent,
        dispatcher_percent=cfg.dispatcher_percent,
        fuel_target_price=cfg.fuel_target_price,

        dollars_per_mile_month=dollars_per_mile_month,
        avg_fuel_price_month=avg_fuel_price_month,
        result_percent_month=result_percent_month,

        mpg_month=dollars_per_mile_month,

        chart_weeks_labels=weeks_labels,
        chart_weeks_revenue=weeks_revenue,
        chart_weeks_expenses=weeks_expenses,
        chart_weeks_net=weeks_net,
        chart_weeks_dpm=weeks_dpm,
        chart_weeks_avg_fuel=weeks_avg_fuel,
        chart_weeks_fuel_target=weeks_fuel_target,
        chart_weeks_result_pct=weeks_result_pct,
        chart_weeks_fuel=chart_weeks_fuel,

        chart_across_labels=across_labels,
        chart_across_revenue=across_revenue,
        chart_across_expenses=across_expenses,
        chart_across_net=across_net,

        chart_month_labels=chart_month_labels,
        chart_month_net=chart_month_net,

        chart_pie_labels=pie_labels,
        chart_pie_values=pie_values,

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
        (
            cfg,
            fixed_month_total,
            weeks_rows,
            week_cards,
            totals,
            dollars_per_mile_month,
            avg_fuel_price_month,
            result_percent_month,
        ) = compute_month_aggregate(yy, mm)

        rows.append({
            "year": yy,
            "month": mm,
            "label": f"{yy}-{mm:02d}",
            "weeks_in_month": cfg.weeks_in_month,
            "fixed_month_total": fixed_month_total,
            "revenue": totals["revenue"],
            "expenses": totals["expenses"],
            "net": totals["net"],
            "dollars_per_mile_month": dollars_per_mile_month,
            "avg_fuel_price_month": avg_fuel_price_month,
            "result_percent_month": result_percent_month,
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

        if action == "save_finance":
            driver_percent = _to_float(request.form.get("driver_percent"), 30.0)
            dispatcher_percent = _to_float(request.form.get("dispatcher_percent"), 10.0)
            fuel_target_price = _to_float(request.form.get("fuel_target_price"), 3.30)

            if driver_percent < 0:
                driver_percent = 0.0
            if dispatcher_percent < 0:
                dispatcher_percent = 0.0
            if fuel_target_price < 0:
                fuel_target_price = 0.0

            cfg.driver_percent = driver_percent
            cfg.dispatcher_percent = dispatcher_percent
            cfg.fuel_target_price = fuel_target_price
            db.session.commit()

            flash("Configurações financeiras salvas.", "success")
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
        year=year,
        month=month,
        weeks_in_month=cfg.weeks_in_month,
        fixed_total=fixed_total,
        fixed_rows=fixed_rows,
        driver_percent=cfg.driver_percent,
        dispatcher_percent=cfg.dispatcher_percent,
        fuel_target_price=cfg.fuel_target_price,
    )


ADMIN_CADASTRO_BASE = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{{ titulo }}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
  <style>
    body{margin:0;background:#070b16;color:#e5e7eb;font-family:Arial,sans-serif}
    .page{padding:22px;min-height:100vh;box-sizing:border-box}
    .shell{display:grid;grid-template-columns:340px 1fr;gap:18px}
    .hero,.content{
      border-radius:24px;
      border:1px solid rgba(148,163,184,0.12);
      background:linear-gradient(180deg, rgba(2,6,23,0.88), rgba(15,23,42,0.82));
      box-shadow:0 22px 60px rgba(0,0,0,0.28), inset 0 1px 0 rgba(255,255,255,0.03);
    }
    .hero{padding:30px;min-height:620px;display:flex;flex-direction:column;justify-content:space-between}
    .content{padding:18px}
    .hero h1{margin:0 0 12px;font-size:48px;line-height:.95}
    .hero p{margin:0;color:#cbd5e1;line-height:1.6}
    .hero-actions{display:flex;flex-direction:column;gap:12px}
    .hero-btn,.btn,.btn-danger{
      display:inline-flex;align-items:center;justify-content:center;
      padding:13px 18px;border-radius:999px;text-decoration:none;font-weight:900;border:none;cursor:pointer
    }
    .hero-btn,.btn{
      color:#06110a;background:linear-gradient(135deg, rgba(34,197,94,1), rgba(34,197,94,0.78))
    }
    .btn-outline{
      background:rgba(15,23,42,0.55);color:#e2e8f0;border:1px solid rgba(148,163,184,0.16)
    }
    .btn-danger{
      color:#fecaca;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.26)
    }
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .panel{
      border:1px solid rgba(148,163,184,.12);
      border-radius:20px;
      padding:18px;
      background:linear-gradient(180deg, rgba(2,6,23,.18), rgba(15,23,42,.24));
    }
    .panel h3{margin:0 0 10px}
    .muted{color:#94a3b8}
    .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
    .field{flex:1 1 220px}
    .field label{display:block;font-size:12px;color:#94a3b8;margin-bottom:6px}
    .input{
      width:100%;box-sizing:border-box;background:#020817;color:#fff;border:1px solid rgba(148,163,184,.16);
      border-radius:14px;padding:12px 14px
    }
    .table{width:100%;border-collapse:collapse}
    .table th,.table td{padding:12px 10px;border-bottom:1px solid rgba(148,163,184,.12);text-align:left}
    .table th{font-size:12px;color:#94a3b8}
    .actions{display:flex;gap:10px;flex-wrap:wrap}
    .flash-area{margin-bottom:14px}
    .flash{padding:12px 14px;border-radius:14px;margin-bottom:8px;font-weight:700}
    .flash.success{background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(34,197,94,.24)}
    .flash.error{background:rgba(239,68,68,.12);color:#fecaca;border:1px solid rgba(239,68,68,.24)}
    @media (max-width: 1000px){
      .shell,.grid{grid-template-columns:1fr}
      .hero{min-height:auto}
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="shell">
      <div class="hero">
        <div>
          <h1>{{ titulo }}</h1>
          <p>{{ subtitulo }}</p>
        </div>

        <div class="hero-actions">
          <a class="hero-btn btn-outline" href="{{ url_for('dashboard') }}">← Voltar</a>
          <a class="hero-btn btn-outline" href="{{ url_for('logout') }}">Sair</a>
        </div>
      </div>

      <div class="content">
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-area">
              {% for cat, msg in messages %}
                <div class="flash {{ cat }}">{{ msg }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="panel" style="margin-bottom:16px;">
          <h3>Novo cadastro</h3>
          <form method="post">
            <input type="hidden" name="action" value="add">

            <div class="row">
              <div class="field">
                <label>Nome</label>
                <input class="input" type="text" name="nome" required>
              </div>

              <div class="field">
                <label>{{ campo2_label }}</label>
                <input class="input" type="text" name="campo2">
              </div>

              <div class="field">
                <label>{{ campo3_label }}</label>
                <input class="input" type="text" name="campo3">
              </div>
            </div>

            <div class="row" style="margin-top:12px;">
              <div class="field" style="flex:1 1 100%;">
                <label>Observações</label>
                <textarea class="input" name="observacoes" rows="3"></textarea>
              </div>
            </div>

            <div class="row" style="margin-top:12px;">
              <button class="btn" type="submit">Salvar cadastro</button>
            </div>
          </form>
        </div>

        <div class="panel">
          <h3>Cadastros salvos</h3>

          {% if rows %}
            <table class="table">
              <thead>
                <tr>
                  <th>Nome</th>
                  <th>{{ campo2_label }}</th>
                  <th>{{ campo3_label }}</th>
                  <th>Observações</th>
                  <th style="text-align:right;">Ações</th>
                </tr>
              </thead>
              <tbody>
                {% for r in rows %}
                <tr>
                  <td><b>{{ r.nome }}</b></td>
                  <<td>{{ getattr(r, campo2_attr) if campo2_attr else '--' }}</td>
                  <td>{{ getattr(r, campo3_attr) if campo3_attr else '--' }}</td>
                  <td>{{ r.observacoes or '--' }}</td>
                  <td style="text-align:right;">
                    <form method="post" style="display:inline;">
                      <input type="hidden" name="action" value="delete">
                      <input type="hidden" name="item_id" value="{{ r.id }}">
                      <button class="btn-danger" type="submit">Remover</button>
                    </form>
                  </td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          {% else %}
            <p class="muted">Nenhum cadastro ainda.</p>
          {% endif %}
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.route("/admin/motoristas", methods=["GET", "POST"])
@login_required
@admin_required
def admin_motoristas():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            nome = (request.form.get("nome") or "").strip()
            telefone = (request.form.get("campo2") or "").strip() or None
            observacoes = (request.form.get("observacoes") or "").strip() or None

            if not nome:
                flash("Informe o nome do motorista.", "error")
                return redirect(url_for("admin_motoristas"))

            row = Motorista(
                nome=nome,
                telefone=telefone,
                observacoes=observacoes,
            )
            db.session.add(row)
            db.session.commit()
            flash("Motorista cadastrado com sucesso.", "success")
            return redirect(url_for("admin_motoristas"))

        if action == "delete":
            item_id = _to_int(request.form.get("item_id"), 0)
            row = Motorista.query.get(item_id)
            if row:
                db.session.delete(row)
                db.session.commit()
                flash("Motorista removido.", "success")
            return redirect(url_for("admin_motoristas"))

    rows = Motorista.query.order_by(Motorista.nome.asc()).all()

    return render_template_string(
        ADMIN_CADASTRO_BASE,
        titulo="Motoristas",
        subtitulo="Cadastre os motoristas da operação para usar depois nos relatórios e fechamentos.",
        rows=rows,
        campo2_label="Telefone",
        campo3_label="—",
        campo2_attr="telefone",
        campo3_attr=None,
        getattr=getattr,
    )


@app.route("/admin/dispatchers", methods=["GET", "POST"])
@login_required
@admin_required
def admin_dispatchers():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            nome = (request.form.get("nome") or "").strip()
            telefone = (request.form.get("campo2") or "").strip() or None
            observacoes = (request.form.get("observacoes") or "").strip() or None

            if not nome:
                flash("Informe o nome do dispatcher.", "error")
                return redirect(url_for("admin_dispatchers"))

            row = Dispatcher(
                nome=nome,
                telefone=telefone,
                observacoes=observacoes,
            )
            db.session.add(row)
            db.session.commit()
            flash("Dispatcher cadastrado com sucesso.", "success")
            return redirect(url_for("admin_dispatchers"))

        if action == "delete":
            item_id = _to_int(request.form.get("item_id"), 0)
            row = Dispatcher.query.get(item_id)
            if row:
                db.session.delete(row)
                db.session.commit()
                flash("Dispatcher removido.", "success")
            return redirect(url_for("admin_dispatchers"))

    rows = Dispatcher.query.order_by(Dispatcher.nome.asc()).all()

    return render_template_string(
        ADMIN_CADASTRO_BASE,
        titulo="Dispatchers",
        subtitulo="Cadastre os dispatchers da empresa para usar depois no relatório mensal e semanal.",
        rows=rows,
        campo2_label="Telefone",
        campo3_label="—",
        campo2_attr="telefone",
        campo3_attr=None,
        getattr=getattr,
    )


@app.route("/admin/veiculos", methods=["GET", "POST"])
@login_required
@admin_required
def admin_veiculos():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            nome = (request.form.get("nome") or "").strip()
            placa = (request.form.get("campo2") or "").strip() or None
            modelo = (request.form.get("campo3") or "").strip() or None
            observacoes = (request.form.get("observacoes") or "").strip() or None

            if not nome:
                flash("Informe o nome do veículo.", "error")
                return redirect(url_for("admin_veiculos"))

            row = Veiculo(
                nome=nome,
                placa=placa,
                modelo=modelo,
                observacoes=observacoes,
            )
            db.session.add(row)
            db.session.commit()
            flash("Veículo cadastrado com sucesso.", "success")
            return redirect(url_for("admin_veiculos"))

        if action == "delete":
            item_id = _to_int(request.form.get("item_id"), 0)
            row = Veiculo.query.get(item_id)
            if row:
                db.session.delete(row)
                db.session.commit()
                flash("Veículo removido.", "success")
            return redirect(url_for("admin_veiculos"))

    rows = Veiculo.query.order_by(Veiculo.nome.asc()).all()

    return render_template_string(
        ADMIN_CADASTRO_BASE,
        titulo="Veículos / Frota",
        subtitulo="Cadastre os veículos da frota para usar depois nos relatórios com nome, placa e modelo.",
        rows=rows,
        campo2_label="Placa",
        campo3_label="Modelo",
        campo2_attr="placa",
        campo3_attr="modelo",
        getattr=getattr,
    )


# =========================
# INIT DATABASE / ADMIN
# =========================
with app.app_context():
    db.create_all()
    ensure_schema_updates()

    if not User.query.filter_by(email="admin@sistema.com").first():
        admin = User(
            nome="Administrador",
            email="admin@sistema.com",
            senha=generate_password_hash("123456"),
            tipo="admin",
        )
        db.session.add(admin)
        db.session.commit()


# =========================
# RUN LOCAL
# =========================
import os

if __name__ == "__main__":
    print(">>> INICIANDO FLASK AGORA...")
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=True, host="0.0.0.0", port=port, use_reloader=False)