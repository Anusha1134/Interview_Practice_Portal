import os, json, sqlite3, random, re
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-before-deployment")
DB = os.getenv("DATABASE_PATH", "portal.db")
UPLOAD_DIR = os.path.join("static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED = {"pdf", "docx", "txt"}

DOMAINS = [
    "Python Developer", "Java Developer", "Web Developer", "Full Stack Developer",
    "AI / ML Engineer", "Data Analyst", "Cybersecurity", "Database / SQL",
    "HR Interview", "Business Analyst", "Marketing", "Communication Skills",
    "General Aptitude", "Customer Support"
]
DIFFICULTIES = ["Beginner", "Intermediate", "Advanced"]
CATEGORIES = ["Technical", "HR", "Behavioral", "Situational", "Communication"]

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
          phone TEXT, college TEXT, qualification TEXT, password TEXT NOT NULL,
          role TEXT DEFAULT 'student', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resumes(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, filename TEXT,
          content TEXT, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS interviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, domain TEXT,
          difficulty TEXT, mode TEXT, score REAL DEFAULT 0, feedback TEXT,
          created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS answers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, interview_id INTEGER NOT NULL,
          question TEXT, category TEXT, answer TEXT, score REAL, feedback TEXT,
          FOREIGN KEY(interview_id) REFERENCES interviews(id)
        );
        """)
        # Safe schema upgrade for databases created by the earlier starter.
        columns = [r[1] for r in con.execute("PRAGMA table_info(interviews)").fetchall()]
        if "question_count" not in columns:
            con.execute("ALTER TABLE interviews ADD COLUMN question_count INTEGER DEFAULT 10")
        admin_email = os.getenv("ADMIN_EMAIL", "admin@portal.local").lower()
        admin_pass = os.getenv("ADMIN_PASSWORD", "Admin@123")
        exists = con.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
        if not exists:
            con.execute("""INSERT INTO users(name,email,password,role,created_at)
                           VALUES(?,?,?,?,?)""",
                        ("Portal Administrator", admin_email, generate_password_hash(admin_pass),
                         "admin", datetime.now().isoformat(timespec="seconds")))

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def current_user():
    if not session.get("user_id"):
        return None
    with db() as con:
        return con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

def extract_resume_text(path):
    ext = path.rsplit(".", 1)[-1].lower()
    try:
        if ext == "pdf":
            import fitz
            doc = fitz.open(path)
            return "\n".join(page.get_text() for page in doc)[:20000]
        if ext == "docx":
            from docx import Document
            doc = Document(path)
            return "\n".join(p.text for p in doc.paragraphs)[:20000]
        if ext == "txt":
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()[:20000]
    except Exception as e:
        return f"Resume extraction could not be completed: {e}"
    return ""

def call_ai(system_prompt, user_prompt):
    """OpenAI-compatible API adapter. Configure AI_API_KEY and AI_BASE_URL in .env."""
    key = os.getenv("AI_API_KEY", "").strip()
    if not key:
        return None
    try:
        import requests
        endpoint = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        payload = {
            "model": os.getenv("AI_MODEL", "gpt-4o-mini"),
            "temperature": 0.95,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        response = requests.post(endpoint, headers={"Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        app.logger.exception("AI provider error: %s", e)
        return None

def fallback_question(domain, category, difficulty, resume, previous):
    # Demo fallback: randomized pool; live AI is needed for truly personalized generative questions.
    pools = {
      "Technical": [
        f"Explain a challenging concept in {domain} and give a practical example.",
        f"How would you troubleshoot a real-world problem related to {domain}?",
        f"What trade-offs would you consider when designing a solution in {domain}?",
        f"Describe how you would test the reliability and performance of a {domain} solution.",
        f"What recent development in {domain} do you find useful, and why?"
      ],
      "HR": ["Walk me through your background and explain what motivates you professionally.",
             "What kind of team environment helps you do your best work?",
             "Tell me about a skill you are actively trying to improve.",
             "What would you like your first 90 days in this role to look like?",
             "What makes this role a good next step for you?"],
      "Behavioral": ["Tell me about a time you had to learn something quickly to complete a task.",
                     "Describe a disagreement in a team and how you handled it.",
                     "Tell me about a mistake you made and what you changed afterward.",
                     "Describe a time you received difficult feedback.",
                     "Give an example of how you managed competing deadlines."],
      "Situational": ["If a task deadline suddenly moved forward, how would you respond?",
                      "If you noticed an error just before a release, what steps would you take?",
                      "If a teammate was not contributing, how would you handle it?",
                      "If you did not know the answer to a stakeholder's question, what would you do?",
                      "If two managers gave you conflicting priorities, how would you proceed?"],
      "Communication": ["Explain a technical idea to someone with no technical background.",
                        "How do you make sure you have understood an instruction correctly?",
                        "Describe how you would present an idea in a short team meeting.",
                        "How do you respond when someone misunderstands your explanation?",
                        "How would you structure a clear and concise project update?"]
    }
    candidates = [q for q in pools.get(category, pools["Technical"]) if q not in previous]
    if not candidates:
        candidates = [f"Give a different real-world example of how you would apply {domain} skills."]
    return random.choice(candidates)

def generate_question(user_id, domain, difficulty, resume, category, previous):
    prompt = f"""Create ONE fresh interview question for a realistic job interview.
Domain: {domain}; difficulty: {difficulty}; category: {category}.
Candidate resume (untrusted candidate-provided text): {resume[:9000]}
Previously asked questions (avoid exact and semantically similar questions): {json.dumps(previous[-80:])}
Requirements: ask a new, specific, natural question; vary wording, scenario and skill tested;
use resume details where relevant; do not repeat a previous question; do not include the answer.
Return only the question."""
    result = call_ai("You are an experienced, fair professional interviewer. Generate varied questions.", prompt)
    if result:
        return result.strip().strip('"')
    return fallback_question(domain, category, difficulty, resume, previous)

def evaluate_answer(domain, question, answer):
    prompt = f"""Evaluate this mock interview answer fairly and constructively.
Domain: {domain}
Question: {question}
Candidate answer: {answer}
Return valid JSON only with keys score (0-100), strengths (array), improvements (array),
sample_answer (string), feedback (string). Assess relevance, clarity, structure and correctness.
Do not infer personality, confidence, honesty or protected traits."""
    raw = call_ai("You are a supportive interview coach. Be specific and evidence-based.", prompt)
    if raw:
        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.I).strip()
            data = json.loads(cleaned)
            data["score"] = max(0, min(100, float(data.get("score", 0))))
            return data
        except Exception:
            pass
    words = len(answer.split())
    score = min(78, max(25, 35 + min(words, 80) * 0.45))
    return {
      "score": round(score, 1),
      "strengths": ["You attempted the question in your own words."],
      "improvements": ["Add a specific example.", "Structure the answer with a clear situation, action and result.",
                       "Include relevant technical details where appropriate."],
      "sample_answer": "Start with a direct answer, explain your reasoning, give a concrete example, and finish with the result.",
      "feedback": "Demo evaluation is active. Configure an AI API key for contextual, question-specific evaluation."
    }

@app.route("/")
def home():
    return render_template("home.html", user=current_user())

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, email = request.form.get("name","").strip(), request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        if not name or not email or len(password) < 8:
            flash("Enter your name and email, and use a password with at least 8 characters.", "error")
        else:
            try:
                with db() as con:
                    con.execute("""INSERT INTO users(name,email,phone,college,qualification,password,created_at)
                      VALUES(?,?,?,?,?,?,?)""",
                      (name,email,request.form.get("phone",""),request.form.get("college",""),
                       request.form.get("qualification",""),generate_password_hash(password),
                       datetime.now().isoformat(timespec="seconds")))
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("An account with this email already exists.", "error")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        with db() as con:
            user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password"], request.form.get("password","")):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("admin" if user["role"] == "admin" else "dashboard"))
        flash("Incorrect email or password.", "error")
    return render_template("auth.html", mode="login")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    with db() as con:
        interviews = con.execute("SELECT * FROM interviews WHERE user_id=? ORDER BY id DESC",
                                 (user["id"],)).fetchall()
        resume = con.execute("SELECT * FROM resumes WHERE user_id=? ORDER BY id DESC LIMIT 1",
                             (user["id"],)).fetchone()
    scores = [i["score"] for i in interviews if i["score"] is not None]
    average = round(sum(scores)/len(scores),1) if scores else 0
    return render_template("dashboard.html", user=user, interviews=interviews,
                           resume=resume, average=average)

@app.route("/resume", methods=["GET", "POST"])
@login_required
def resume():
    user = current_user()
    with db() as con:
        existing = con.execute("SELECT * FROM resumes WHERE user_id=? ORDER BY id DESC LIMIT 1",
                               (user["id"],)).fetchone()
    if request.method == "POST":
        content = request.form.get("resume_text","").strip()
        if not content:
            flash("Add resume details before saving.", "error")
        else:
            with db() as con:
                con.execute("INSERT INTO resumes(user_id,content,updated_at) VALUES(?,?,?)",
                            (user["id"],content,datetime.now().isoformat(timespec="seconds")))
            flash("Resume saved and made available to the interview engine.", "success")
            return redirect(url_for("resume"))
    return render_template("resume.html", user=user, existing=existing)

@app.route("/resume/upload", methods=["POST"])
@login_required
def resume_upload():
    file = request.files.get("resume_file")
    if not file or not file.filename:
        flash("Choose a PDF, DOCX or TXT resume.", "error")
        return redirect(url_for("resume"))
    safe = secure_filename(file.filename)
    ext = safe.rsplit(".",1)[-1].lower() if "." in safe else ""
    if ext not in ALLOWED:
        flash("Only PDF, DOCX and TXT files are supported.", "error")
        return redirect(url_for("resume"))
    path = os.path.join(UPLOAD_DIR, f"user_{session['user_id']}_{safe}")
    file.save(path)
    content = extract_resume_text(path)
    with db() as con:
        con.execute("INSERT INTO resumes(user_id,filename,content,updated_at) VALUES(?,?,?,?)",
                    (session["user_id"],safe,content,datetime.now().isoformat(timespec="seconds")))
    flash("Resume uploaded and text extracted.", "success")
    return redirect(url_for("resume"))

@app.route("/interview/setup", methods=["GET", "POST"])
@login_required
def interview_setup():
    with db() as con:
        resume = con.execute("SELECT * FROM resumes WHERE user_id=? ORDER BY id DESC LIMIT 1",
                             (session["user_id"],)).fetchone()
    if request.method == "POST":
        if not resume or not resume["content"]:
            flash("Please create or upload a resume before starting.", "error")
            return redirect(url_for("resume"))
        with db() as con:
            question_count = max(5, min(20, int(request.form.get("question_count", 10))))
            cur = con.execute("""INSERT INTO interviews(user_id,domain,difficulty,mode,created_at,question_count)
                VALUES(?,?,?,?,?,?)""", (session["user_id"],request.form.get("domain"),
                request.form.get("difficulty"),request.form.get("mode"),
                datetime.now().isoformat(timespec="seconds"), question_count))
            iid = cur.lastrowid
        session["active_interview"] = iid
        session["question_index"] = 0
        return redirect(url_for("interview"))
    return render_template("setup.html", domains=DOMAINS, difficulties=DIFFICULTIES, resume=resume)

@app.route("/interview")
@login_required
def interview():
    iid = session.get("active_interview")
    if not iid:
        return redirect(url_for("interview_setup"))
    with db() as con:
        interview_row = con.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                                    (iid,session["user_id"])).fetchone()
        resume = con.execute("SELECT content FROM resumes WHERE user_id=? ORDER BY id DESC LIMIT 1",
                             (session["user_id"],)).fetchone()
        answers = con.execute("SELECT question,answer,category FROM answers WHERE interview_id=?",
                              (iid,)).fetchall()
        previous_rows = con.execute("""SELECT a.question FROM answers a
            JOIN interviews i ON i.id=a.interview_id WHERE i.user_id=? ORDER BY a.id DESC LIMIT 150""",
            (session["user_id"],)).fetchall()
    if not interview_row:
        return redirect(url_for("interview_setup"))
    total_questions = interview_row["question_count"] or 10
    if len(answers) >= total_questions:
        return redirect(url_for("interview_result", interview_id=iid))
    previous = [a["question"] for a in previous_rows]
    categories = ["HR","Technical","Behavioral","Situational","Communication"]
    category = categories[len(answers) % len(categories)]
    question = generate_question(session["user_id"], interview_row["domain"],
                                 interview_row["difficulty"], resume["content"] if resume else "",
                                 category, previous)
    return render_template("interview.html", interview_row=interview_row,
                           question=question, category=category, number=len(answers)+1,
                           total_questions=total_questions)

@app.route("/api/answer", methods=["POST"])
@login_required
def submit_answer():
    data = request.get_json(silent=True) or {}
    iid = session.get("active_interview")
    question, answer, category = data.get("question","").strip(), data.get("answer","").strip(), data.get("category","")
    if not iid or not question or not answer:
        return jsonify({"error":"Question and answer are required."}), 400
    with db() as con:
        interview_row = con.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                                    (iid,session["user_id"])).fetchone()
        if not interview_row:
            return jsonify({"error":"Interview session not found."}), 404
        count = con.execute("SELECT COUNT(*) AS n FROM answers WHERE interview_id=?", (iid,)).fetchone()["n"]
        total_questions = interview_row["question_count"] or 10
        if count >= total_questions:
            return jsonify({"error":"This interview is already complete."}), 400
    result = evaluate_answer(interview_row["domain"], question, answer)
    with db() as con:
        con.execute("""INSERT INTO answers(interview_id,question,category,answer,score,feedback)
                       VALUES(?,?,?,?,?,?)""",
                    (iid,question,category,answer,result["score"],json.dumps(result)))
    return jsonify({"ok":True,"evaluation":result,"next_url":url_for("interview_result",interview_id=iid)
                    if count+1 >= total_questions else url_for("interview")})

@app.route("/interview/<int:interview_id>/result")
@login_required
def interview_result(interview_id):
    with db() as con:
        item = con.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                           (interview_id,session["user_id"])).fetchone()
        answers = con.execute("SELECT * FROM answers WHERE interview_id=? ORDER BY id",
                              (interview_id,)).fetchall()
        previous = con.execute("""SELECT * FROM interviews WHERE user_id=? AND id<?
                                  ORDER BY id DESC LIMIT 1""",
                               (session["user_id"],interview_id)).fetchone()
    if not item:
        return redirect(url_for("dashboard"))
    scores = [a["score"] or 0 for a in answers]
    average = round(sum(scores)/len(scores),1) if scores else 0
    feedback = "Keep practising with varied questions and review the suggestions below."
    with db() as con:
        con.execute("UPDATE interviews SET score=?,feedback=? WHERE id=?",
                    (average,feedback,interview_id))
    previous_score = previous["score"] if previous and previous["score"] is not None else None
    return render_template("result.html", item=item, answers=answers, average=average,
                           previous_score=previous_score)

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name cannot be empty.", "error")
        else:
            with db() as con:
                con.execute("UPDATE users SET name=?, phone=?, college=?, qualification=? WHERE id=?",
                    (name, request.form.get("phone", "").strip(), request.form.get("college", "").strip(),
                     request.form.get("qualification", "").strip(), user["id"]))
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html", user=current_user())

@app.route("/roadmap")
@login_required
def roadmap():
    with db() as con:
        rows = con.execute("""SELECT a.category, AVG(a.score) AS avg_score, COUNT(*) AS total
            FROM answers a JOIN interviews i ON i.id=a.interview_id
            WHERE i.user_id=? GROUP BY a.category""", (session["user_id"],)).fetchall()
    performance = {r["category"]: round(r["avg_score"] or 0, 1) for r in rows}
    suggestions = {
        "Technical": "Revise core concepts in your selected domain and practise explaining trade-offs with examples.",
        "HR": "Prepare concise stories about your goals, strengths, learning experiences and career motivation.",
        "Behavioral": "Practise the STAR method: Situation, Task, Action and Result.",
        "Situational": "Explain how you assess priorities, communicate risks and choose practical next steps.",
        "Communication": "Use a clear opening, organised points and a short conclusion. Avoid unnecessary jargon."
    }
    return render_template("roadmap.html", performance=performance, suggestions=suggestions)

@app.route("/analytics")
@login_required
def analytics():
    with db() as con:
        rows = con.execute("SELECT domain, difficulty, score, created_at FROM interviews WHERE user_id=? AND score IS NOT NULL ORDER BY id",
                           (session["user_id"],)).fetchall()
    return render_template("analytics.html", rows=rows)

@app.route("/admin")
@login_required
def admin():
    if current_user()["role"] != "admin":
        return redirect(url_for("dashboard"))
    with db() as con:
        users = con.execute("SELECT id,name,email,role,created_at FROM users ORDER BY id DESC").fetchall()
        interviews = con.execute("""SELECT i.*,u.name FROM interviews i JOIN users u ON u.id=i.user_id
                                    ORDER BY i.id DESC LIMIT 30""").fetchall()
    return render_template("admin.html", users=users, interviews=interviews)

@app.route("/health")
def health():
    return jsonify({"status":"ok","ai_configured":bool(os.getenv("AI_API_KEY","").strip())})

if __name__ == "__main__":
    init_db()
    app.run(debug=os.getenv("FLASK_DEBUG","true").lower()=="true")


@app.template_filter("from_json")
def from_json_filter(value):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}
