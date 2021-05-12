import cv2 as cv
import numpy as np


def optimize_img(cv_img: np.ndarray) -> np.ndarray:
    cv_bl = cv.GaussianBlur(cv_img, (3, 3), p1)
    edged = cv.Canny(cv_bl, p2, p3)
    kernel = cv.getStructuringElement(cv.MORPH_RECT, (7, 7))
    closed = cv.morphologyEx(edged, cv.MORPH_CLOSE, kernel)

    return closed


def update_p1(v):
    global p1
    p1 = v
    update()


def update_p2(v):
    global p2
    p2 = v
    update()


def update_p3(v):
    global p3
    p3 = v
    update()


def update():
    res_img = img.copy()
    res_img = optimize_img(res_img)
    cv.imshow('res', res_img)


if __name__ == '__main__':
    img = cv.imread("imgs\\1.jpg", cv.IMREAD_GRAYSCALE)
    p1 = 1
    p2 = 5
    p3 = 1

    update_p1(0)
    update_p2(10)
    update_p3(250)

    cv.createTrackbar("p1", "res", 0, 10, update_p1)
    cv.createTrackbar("p2", "res", 10, 255, update_p2)
    cv.createTrackbar("p3", "res", 250, 255, update_p3)

    cv.waitKey()
    cv.destroyAllWindows()
