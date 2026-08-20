"""
Mock legacy core-banking app.

Deliberately hostile markup (matches the assignment's "real environment"
description): no data-testid anywhere, non-semantic class names, a nested
table layout for search results, and the confirmation step rendered inside
an iframe. The accessibility tree (role + accessible name) is still clean
because we use proper semantic HTML elements (real <button>, <label for=...>,
<table><th>) - ugly CSS/class names, not ugly accessibility.
"""
from flask import Flask, render_template, request, redirect, url_for
import models

app = Flask(__name__)


@app.route("/")
def home():
    return redirect(url_for("search"))


@app.route("/search", methods=["GET", "POST"])
def search():
    results = None
    query = ""
    if request.method == "POST":
        query = request.form.get("q", "").strip()
        results = models.search_members(query) if query else []
    return render_template("search.html", results=results, query=query)


@app.route("/member/<member_id>")
def member_detail(member_id):
    member = models.find_member(member_id)
    return render_template("member_detail.html", member=member, member_id=member_id)


@app.route("/member/<member_id>/new-subaccount", methods=["GET", "POST"])
def new_subaccount(member_id):
    member = models.find_member(member_id)
    if not member:
        return render_template("member_detail.html", member=None, member_id=member_id)
    if member["status"] == "locked":
        return render_template("error_permission.html", member_id=member_id)

    if request.method == "POST":
        account_type = request.form.get("account_type")
        nickname = request.form.get("nickname", "")
        deposit_dollars = request.form.get("opening_deposit", "0")
        try:
            deposit_cents = int(float(deposit_dollars) * 100)
        except ValueError:
            deposit_cents = 0
        return render_template(
            "confirm_wrapper.html",
            member=member,
            account_type=account_type,
            nickname=nickname,
            deposit_cents=deposit_cents,
        )
    return render_template("new_subaccount.html", member=member)


@app.route("/member/<member_id>/confirm-frame")
def confirm_frame(member_id):
    member = models.find_member(member_id)
    return render_template(
        "confirm_subaccount.html",
        member=member,
        account_type=request.args.get("account_type"),
        nickname=request.args.get("nickname"),
        deposit_cents=int(request.args.get("deposit_cents", "0")),
    )


@app.route("/member/<member_id>/new-subaccount/submit", methods=["POST"])
def submit_subaccount(member_id):
    member = models.find_member(member_id)
    if not member:
        return render_template("member_detail.html", member=None, member_id=member_id)
    account_type = request.form.get("account_type")
    nickname = request.form.get("nickname", "")
    deposit_cents = int(request.form.get("deposit_cents", "0"))
    new_id = models.create_subaccount(member_id, account_type, nickname, deposit_cents)
    return render_template(
        "subaccount_success.html", member=member, subaccount_id=new_id, account_type=account_type
    )


@app.route("/member/<member_id>/transactions")
def transactions(member_id):
    member = models.find_member(member_id)
    if not member:
        return render_template("member_detail.html", member=None, member_id=member_id)
    if member["status"] == "locked":
        return render_template("error_permission.html", member_id=member_id)
    txns = models.list_transactions(member_id)
    return render_template("transactions.html", member=member, transactions=txns)


@app.route("/member/<member_id>/transactions/<int:transaction_id>/dispute", methods=["GET", "POST"])
def dispute_transaction(member_id, transaction_id):
    member = models.find_member(member_id)
    if not member:
        return render_template("member_detail.html", member=None, member_id=member_id)
    if member["status"] == "locked":
        return render_template("error_permission.html", member_id=member_id)
    txn = models.get_transaction(transaction_id)
    if not txn or txn["member_id"] != member_id:
        return render_template("member_detail.html", member=None, member_id=member_id)

    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        models.dispute_transaction(transaction_id, reason)
        txn = models.get_transaction(transaction_id)
        return render_template("dispute_success.html", member=member, transaction=txn)

    return render_template("dispute_transaction.html", member=member, transaction=txn)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
