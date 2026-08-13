/**
 * Express routes — known ground truth for endpoint extraction tests
 * (SPEC.md Phase 2 acceptance criterion: >=90% of real endpoints extracted
 * with correct method, path, and handler location).
 *
 * Ground truth (declared paths, not merged with the app.use() mount prefix
 * below — see DECISIONS.md for why mount-prefix merging is out of scope):
 *   GET  /health   -> app.get,  anonymous handler
 *   POST /login    -> router.post, anonymous handler
 *   GET  /users/:id -> router.get, anonymous handler, has requireAuth middleware
 */

import express from "express";
import { requireAuth } from "./authMiddleware";

const app = express();
const router = express.Router();

app.get("/health", (req, res) => {
  res.send("ok");
});

router.post("/login", (req, res) => {
  res.send("logged in");
});

router.get("/users/:id", requireAuth, (req, res) => {
  res.json({ id: req.params.id });
});

app.use("/api", router);

export default app;
