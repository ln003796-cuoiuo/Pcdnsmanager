package com.localai.chat

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.view.Gravity
import android.view.ViewGroup
import android.widget.*
import com.google.mediapipe.tasks.genai.llminference.LlmInference
import java.io.File
import kotlin.concurrent.thread

class MainActivity : Activity() {

    private val ui = Handler(Looper.getMainLooper())
    private var llm: LlmInference? = null
    private lateinit var kb: KnowledgeBase
    private lateinit var log: TextView
    private lateinit var scroll: ScrollView
    private lateinit var input: EditText
    private lateinit var send: Button
    private lateinit var status: TextView
    private val turns = ArrayList<Pair<String, String>>() // (вопрос, ответ)
    private val modelFile get() = File(filesDir, "model.task")
    private var chatml = false // true для Qwen (шаблон ChatML), false для Gemma
    private val prefs by lazy { getSharedPreferences("cfg", MODE_PRIVATE) }
    private lateinit var tmplBtn: Button
    private fun tmplLabel() = if (chatml) "Qwen ⇄" else "Gemma ⇄"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        kb = KnowledgeBase(File(filesDir, "kb"))
        chatml = prefs.getBoolean("chatml", false)
        val d = resources.displayMetrics.density
        val pad = (12 * d).toInt()

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 2, pad, pad)
        }
        status = TextView(this).apply { textSize = 13f; setTextColor(Color.DKGRAY) }

        fun btn(t: String, f: () -> Unit) = Button(this).apply {
            text = t; textSize = 12f; isAllCaps = false; setOnClickListener { f() }
        }
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            val lp = { LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f) }
            addView(btn("Модель") { pickModel() }, lp())
            addView(btn("+ txt") { pickTxt() }, lp())
            addView(btn("Очистить") { clearKb() }, lp())
            tmplBtn = btn(tmplLabel()) {
                chatml = !chatml
                prefs.edit().putBoolean("chatml", chatml).apply()
                tmplBtn.text = tmplLabel()
            }
            addView(tmplBtn, lp())
        }

        log = TextView(this).apply { textSize = 16f; setTextColor(Color.BLACK); setTextIsSelectable(true) }
        scroll = ScrollView(this).apply { addView(log) }
        input = EditText(this).apply { hint = "Сообщение"; maxLines = 4 }
        send = Button(this).apply { text = "➤"; setOnClickListener { sendMessage() } }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(input, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(send)
        }
        root.addView(status)
        root.addView(bar)
        root.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        root.addView(row)
        setContentView(root)

        if (modelFile.exists()) loadModel() else {
            setStatus("Модель не загружена. Нажми «Модель» и выбери файл.")
            send.isEnabled = false
        }
    }

    private fun setStatus(s: String) {
        status.text = s + "\nЗнания: ${kb.fileCount} файл(ов), ${kb.chunkCount} фрагментов"
    }

    private fun pickModel() {
        startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE); type = "*/*"
        }, 1)
    }

    private fun pickTxt() {
        startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE); type = "text/*"
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        }, 2)
    }

    private fun clearKb() {
        kb.clear()
        setStatus(if (llm != null) "Модель готова (офлайн)" else "Модель не загружена")
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK || data == null) return
        if (requestCode == 1) importModel(data.data ?: return)
        if (requestCode == 2) {
            val uris = ArrayList<Uri>()
            data.clipData?.let { c -> for (i in 0 until c.itemCount) uris.add(c.getItemAt(i).uri) }
            data.data?.let { if (uris.isEmpty()) uris.add(it) }
            importTxt(uris)
        }
    }

    private fun displayName(u: Uri): String {
        var name = "file"
        contentResolver.query(u, null, null, null, null)?.use {
            if (it.moveToFirst()) {
                val i = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (i >= 0) name = it.getString(i)
            }
        }
        return name
    }

    private fun importModel(uri: Uri) {
        // Шаблон чата выбирается по имени файла: qwen -> ChatML, иначе Gemma
        chatml = displayName(uri).lowercase().contains("qwen")
        prefs.edit().putBoolean("chatml", chatml).apply()
        tmplBtn.text = tmplLabel()
        send.isEnabled = false
        setStatus("Копирую модель…")
        thread {
            try {
                llm?.close(); llm = null
                contentResolver.openInputStream(uri)!!.use { inp ->
                    modelFile.outputStream().use { out -> inp.copyTo(out, 1 shl 20) }
                }
                ui.post { loadModel() }
            } catch (e: Exception) {
                ui.post { setStatus("Ошибка копирования: ${e.message}") }
            }
        }
    }

    private fun importTxt(uris: List<Uri>) {
        setStatus("Читаю файлы…")
        thread {
            try {
                for (u in uris) {
                    var name = "file.txt"
                    contentResolver.query(u, null, null, null, null)?.use {
                        if (it.moveToFirst()) {
                            val i = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                            if (i >= 0) name = it.getString(i)
                        }
                    }
                    val text = contentResolver.openInputStream(u)!!.use { it.readBytes().toString(Charsets.UTF_8) }
                    kb.add(name, text)
                }
                ui.post { setStatus(if (llm != null) "Модель готова (офлайн)" else "Модель не загружена") }
            } catch (e: Exception) {
                ui.post { setStatus("Ошибка чтения: ${e.message}") }
            }
        }
    }

    private fun loadModel() {
        setStatus("Загружаю модель…")
        send.isEnabled = false
        thread {
            try {
                val opts = LlmInference.LlmInferenceOptions.builder()
                    .setModelPath(modelFile.absolutePath)
                    .setMaxTokens(2048)
                    .build()
                llm = LlmInference.createFromOptions(this, opts)
                ui.post { setStatus("Модель готова (офлайн)"); send.isEnabled = true }
            } catch (e: Exception) {
                ui.post { setStatus("Не удалось загрузить модель: ${e.message}") }
            }
        }
    }

    private fun buildPrompt(question: String): String {
        val u1 = if (chatml) "<|im_start|>user\n" else "<start_of_turn>user\n"
        val e1 = if (chatml) "<|im_end|>\n" else "<end_of_turn>\n"
        val m1 = if (chatml) "<|im_start|>assistant\n" else "<start_of_turn>model\n"
        val sb = StringBuilder()
        for ((q, a) in turns.takeLast(2)) {
            sb.append(u1).append(q).append(e1)
            sb.append(m1).append(a.take(500)).append(e1)
        }
        sb.append(u1)
        val ctx = kb.search(question, 3)
        if (ctx.isNotEmpty()) {
            sb.append("Используй эти заметки для ответа, если они относятся к вопросу:\n")
            ctx.forEachIndexed { i, c -> sb.append("[${i + 1}] ${c.text}\n") }
            sb.append("\nВопрос: ")
        }
        sb.append(question).append(e1).append(m1)
        return sb.toString()
    }

    private fun sendMessage() {
        val text = input.text.toString().trim()
        val engine = llm ?: return
        if (text.isEmpty()) return
        input.setText("")
        send.isEnabled = false
        log.append("Ты: $text\n\nИИ: …\n\n")
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
        val prompt = buildPrompt(text)
        thread {
            val answer = try {
                engine.generateResponse(prompt).replace("<end_of_turn>", "").replace("<|im_end|>", "").replace("<|endoftext|>", "").trim()
                    .ifEmpty { "(пустой ответ — нажми кнопку Gemma/Qwen, чтобы сменить шаблон, и повтори)" }
            } catch (e: Exception) {
                "Ошибка: ${e.message}"
            }
            turns.add(text to answer)
            ui.post {
                log.text = log.text.toString().removeSuffix("…\n\n") + answer + "\n\n"
                scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
                send.isEnabled = true
            }
        }
    }

    override fun onDestroy() {
        llm?.close()
        super.onDestroy()
    }
}
