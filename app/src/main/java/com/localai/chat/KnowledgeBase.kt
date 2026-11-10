package com.localai.chat

import java.io.File
import kotlin.math.ln

/** Простая локальная база знаний из txt-файлов: чанки + TF-IDF поиск. */
class KnowledgeBase(private val dir: File) {

    data class Chunk(val src: String, val text: String, val terms: Map<String, Int>)

    private var chunks: List<Chunk> = emptyList()
    private var df: Map<String, Int> = emptyMap()

    val fileCount: Int get() = dir.listFiles()?.size ?: 0
    val chunkCount: Int get() = chunks.size

    init { dir.mkdirs(); reload() }

    fun reload() {
        val list = ArrayList<Chunk>()
        dir.listFiles()?.sortedBy { it.name }?.forEach { f ->
            val text = try { f.readText() } catch (e: Exception) { "" }
            for (part in split(text)) list.add(Chunk(f.name, part, termFreq(part)))
        }
        val d = HashMap<String, Int>()
        for (c in list) for (t in c.terms.keys) d[t] = (d[t] ?: 0) + 1
        chunks = list
        df = d
    }

    fun add(name: String, text: String) {
        val safe = name.replace(Regex("[^\\p{L}\\p{N}._-]"), "_").ifBlank { "file" }
        File(dir, if (safe.endsWith(".txt")) safe else "$safe.txt").writeText(text)
        reload()
    }

    fun clear() {
        dir.listFiles()?.forEach { it.delete() }
        reload()
    }

    fun search(query: String, k: Int = 3): List<Chunk> {
        if (chunks.isEmpty()) return emptyList()
        val q = termFreq(query).keys
        if (q.isEmpty()) return emptyList()
        val n = chunks.size.toDouble()
        return chunks
            .map { c ->
                var s = 0.0
                for (t in q) {
                    val tf = c.terms[t] ?: continue
                    s += (1 + ln(tf.toDouble())) * ln(1 + n / (1 + (df[t] ?: 0)))
                }
                c to s
            }
            .filter { it.second > 0 }
            .sortedByDescending { it.second }
            .take(k)
            .map { it.first }
    }

    private fun termFreq(s: String): Map<String, Int> {
        val m = HashMap<String, Int>()
        for (w in Regex("[\\p{L}\\p{N}]+").findAll(s.lowercase())) {
            val v = w.value
            if (v.length < 3) continue
            val stem = v.take(6) // грубый стемминг для русского
            m[stem] = (m[stem] ?: 0) + 1
        }
        return m
    }

    private fun split(text: String, max: Int = 600, overlap: Int = 100): List<String> {
        val out = ArrayList<String>()
        val buf = StringBuilder()
        fun flush() { if (buf.isNotBlank()) out.add(buf.toString().trim()); buf.setLength(0) }
        for (line in text.lines()) {
            if (line.length > max) {
                flush()
                var i = 0
                while (i < line.length) {
                    out.add(line.substring(i, minOf(line.length, i + max)).trim())
                    i += max - overlap
                }
            } else {
                if (buf.length + line.length + 1 > max) flush()
                buf.append(line).append('\n')
            }
        }
        flush()
        return out.filter { it.isNotBlank() }
    }
}
