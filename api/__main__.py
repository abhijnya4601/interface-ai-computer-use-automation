from api.app import app

if __name__ == "__main__":
    print("[api] capability API + dashboard on http://localhost:8000")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
